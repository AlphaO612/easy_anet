//! Signed, node-specific Prometheus probe revisions shared by the API and agent.
use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[cfg_attr(feature = "poem", derive(poem_openapi::Object))]
pub struct ProbeEdge {
    pub target_node: String,
    pub module: String,
    pub target: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[cfg_attr(feature = "poem", derive(poem_openapi::Object))]
pub struct ProbeRevision {
    pub node_id: String,
    pub revision: i64,
    pub edges: Vec<ProbeEdge>,
    pub signature: String,
}

impl ProbeRevision {
    fn signed_bytes(&self) -> Result<Vec<u8>> {
        Ok(serde_json::to_vec(&(
            "anet/probe-revision/v1",
            &self.node_id,
            self.revision,
            &self.edges,
        ))?)
    }

    pub fn sign(&mut self, key: &SigningKey) -> Result<()> {
        self.validate()?;
        self.signature = STANDARD.encode(key.sign(&self.signed_bytes()?).to_bytes());
        Ok(())
    }

    pub fn verify(&self, key: &VerifyingKey, node_id: &str) -> Result<()> {
        ensure!(self.node_id == node_id, "revision belongs to another node");
        self.validate()?;
        let signature = Signature::from_slice(&STANDARD.decode(&self.signature)?)?;
        key.verify_strict(&self.signed_bytes()?, &signature)?;
        Ok(())
    }

    fn validate(&self) -> Result<()> {
        ensure!(
            self.revision > 0 && self.edges.len() <= 256,
            "invalid revision or edge count"
        );
        ensure!(
            self.edges
                .iter()
                .filter(|e| e.module == "https_capacity")
                .count()
                <= 2,
            "at most two capacity probes per source"
        );
        let mut unique = HashSet::new();
        for edge in &self.edges {
            ensure!(
                !edge.target_node.is_empty()
                    && edge.target_node.len() <= 128
                    && edge
                        .target_node
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c)),
                "invalid target node"
            );
            ensure!(
                edge.target.len() <= 2048 && !edge.target.contains(char::is_whitespace),
                "invalid target"
            );
            match edge.module.as_str() {
                "tcp_connect" => {
                    let (host, port) =
                        edge.target.rsplit_once(':').context("expected host:port")?;
                    ensure!(
                        !host.is_empty()
                            && url::Host::parse(host).is_ok()
                            && port.parse::<u16>().is_ok_and(|p| p != 0),
                        "invalid TCP endpoint"
                    );
                }
                "https_ready" | "https_capacity" => {
                    let url = url::Url::parse(&edge.target)?;
                    if edge.module == "https_capacity" {
                        ensure!(
                            url.path() == "/anet/capacity.bin" && url.query().is_none(),
                            "expected fixed capacity endpoint /anet/capacity.bin"
                        );
                    }
                    ensure!(
                        url.scheme() == "https"
                            && url.host().is_some()
                            && url.username().is_empty()
                            && url.password().is_none()
                            && url.fragment().is_none(),
                        "invalid HTTPS endpoint"
                    );
                }
                _ => anyhow::bail!("unsupported probe module"),
            }
            ensure!(
                unique.insert((&edge.module, &edge.target, &edge.target_node)),
                "duplicate probe"
            );
        }
        Ok(())
    }

    pub fn encode(&self) -> Result<String> {
        Ok(serde_json::to_string(self)?)
    }
    pub fn decode(input: &str) -> Result<Self> {
        Ok(serde_json::from_str(input)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signature_binds_recipient_version_and_endpoints() {
        let key = SigningKey::from_bytes(&[3; 32]);
        let mut revision = ProbeRevision {
            node_id: "one".into(),
            revision: 1,
            signature: String::new(),
            edges: vec![ProbeEdge {
                target_node: "two".into(),
                module: "tcp_connect".into(),
                target: "127.0.0.1:443".into(),
            }],
        };
        revision.sign(&key).unwrap();
        let public = key.verifying_key();
        assert!(revision.verify(&public, "one").is_ok());
        assert!(revision.verify(&public, "two").is_err());
        revision.revision += 1;
        assert!(revision.verify(&public, "one").is_err());
        revision.revision -= 1;
        revision.edges.clear();
        assert!(revision.verify(&public, "one").is_err());
    }
}
