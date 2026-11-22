use crate::config::ControlPlaneConfig;
use anet_common::probe_revision::ProbeRevision;
use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::VerifyingKey;
use reqwest::Client;
use serde_json::json;
use std::io::{ErrorKind, Write};
use std::path::Path;

pub(crate) async fn poll(config: &ControlPlaneConfig, client: &Client) -> Result<()> {
    let Some(dir) = &config.probe_targets_dir else {
        return Ok(());
    };
    let url = format!(
        "{}/control/nodes/{}/probe-revision",
        config.url.trim_end_matches('/'),
        config.node_id
    );
    let response = client
        .get(&url)
        .header("X-Node-Token", &config.token)
        .send()
        .await?;
    if response.status().as_u16() == 404 {
        return Ok(());
    }
    let manifest: ProbeRevision = response.error_for_status()?.json().await?;
    let result = apply(config, dir, &manifest);
    let report = json!({"revision": manifest.revision, "error": result.as_ref().err().map(ToString::to_string)});
    client
        .post(format!("{url}/result"))
        .header("X-Node-Token", &config.token)
        .json(&report)
        .send()
        .await?
        .error_for_status()?;
    result
}

fn apply(config: &ControlPlaneConfig, dir: &Path, manifest: &ProbeRevision) -> Result<()> {
    let public_bytes: [u8; 32] = STANDARD
        .decode(config.probe_signing_public_key.trim())?
        .try_into()
        .map_err(|_| anyhow::anyhow!("invalid probe signing public key"))?;
    let key = VerifyingKey::from_bytes(&public_bytes)?;
    manifest.verify(&key, &config.node_id)?;
    for edge in &manifest.edges {
        ensure!(
            config.probe_allowed_targets.contains(&edge.target),
            "target outside local allowlist"
        );
    }

    let state = dir.join(".anet-probe-revision.json");
    match std::fs::read_to_string(&state) {
        Ok(value) => {
            let previous = ProbeRevision::decode(&value)?;
            previous.verify(&key, &config.node_id)?;
            ensure!(
                manifest.revision > previous.revision
                    || (manifest.revision == previous.revision
                        && manifest.signature == previous.signature),
                "stale or conflicting revision"
            );
        }
        Err(e) if e.kind() == ErrorKind::NotFound => (),
        Err(e) => return Err(e.into()),
    }
    let targets: Vec<_> = manifest.edges.iter().map(|edge| json!({
        "targets": [&edge.target], "labels": {"target_node": &edge.target_node, "module": &edge.module}
    })).collect();
    // Save the signed high-water mark first. Redelivery completes an interrupted targets write.
    replace_if_changed(&state, manifest.encode()?.as_bytes())?;
    replace_if_changed(&dir.join("targets.yml"), &serde_json::to_vec(&targets)?)?;
    Ok(())
}

fn replace_if_changed(path: &Path, bytes: &[u8]) -> Result<()> {
    if std::fs::read(path).is_ok_and(|current| current == bytes) {
        return Ok(());
    }
    let tmp = path.with_extension("anet-tmp");
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    let mut file = opts.open(&tmp).context("opening revision temp file")?;
    file.write_all(bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o644))?;
    }
    file.sync_all()?;
    drop(file);
    std::fs::rename(tmp, path)?;
    #[cfg(unix)]
    std::fs::File::open(path.parent().context("missing target directory")?)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use anet_common::probe_revision::ProbeEdge;
    use ed25519_dalek::SigningKey;

    #[test]
    fn rejected_revision_preserves_live_targets_and_redelivery_repairs_missing_file() {
        let dir = std::env::temp_dir().join(format!(
            "anet-probe-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&dir).unwrap();
        let signer = SigningKey::from_bytes(&[8; 32]);
        let target = "127.0.0.1:2345";
        let config = ControlPlaneConfig {
            node_id: "entry".into(),
            probe_signing_public_key: STANDARD.encode(signer.verifying_key().to_bytes()),
            probe_allowed_targets: vec![target.into()],
            ..Default::default()
        };
        let mut revision = ProbeRevision {
            node_id: "entry".into(),
            revision: 2,
            signature: String::new(),
            edges: vec![ProbeEdge {
                target_node: "exit".into(),
                module: "tcp_connect".into(),
                target: target.into(),
            }],
        };
        revision.sign(&signer).unwrap();
        apply(&config, &dir, &revision).unwrap();
        let targets = dir.join("targets.yml");
        let original = std::fs::read(&targets).unwrap();
        std::fs::remove_file(&targets).unwrap();
        apply(&config, &dir, &revision).unwrap();
        assert_eq!(std::fs::read(&targets).unwrap(), original);
        revision.revision = 1;
        revision.sign(&signer).unwrap();
        assert!(apply(&config, &dir, &revision).is_err());
        revision.revision = 3;
        revision.edges[0].target = "10.0.0.17:22".into();
        revision.sign(&signer).unwrap();
        assert!(apply(&config, &dir, &revision).is_err());
        assert_eq!(std::fs::read(&targets).unwrap(), original);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
