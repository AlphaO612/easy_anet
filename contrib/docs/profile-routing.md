# Profile administration

Open **Profile Topology** in the ANet WebUI using an administrator session. Review field changes before publishing. Each publication creates an immutable database row with the operator, time and revision. A stale base revision returns HTTP 409. Export the published JSON for provisioning. The CLI accepts `python3 contrib/deploy/scripts/topology.py /private/topology.json diff candidate.json` to compare a local copy. The installer does not own the database topology.

```json
{
  "paths": {
    "direct": {"entry":"edge","exit":"edge","external_ip":"192.0.2.1","transport":"wireguard"},
    "chain": {"entry":"edge","exit":"exit","external_ip":"192.0.2.2","transport":"wireguard"}
  },
  "profiles": {
    "alice-mobile": {"user":"alice","entry":"edge","strategy":"prefer-route","paths":["chain","direct"]}
  }
}
```

For `preview session.json` supply fresh observations from the profile's entry. Epoch timestamps use seconds. An absent, down or expired link is unavailable. A trusted administrator supplies the authenticated user and groups. Example session structure (replace timestamps before use):

```json
{"profile":"alice-mobile","user":"alice","groups":["staff"],"destination":"198.51.100.2","metrics":{"chain":{"up":true,"expires":1800000000,"mbps":50,"loss":0,"latency_ms":10}}}
```

The strategies are `strict-ip` (set profile `external_ip`), `prefer-route`, `speed` and `auto`. Supply `previous` and `selected_at` from the previous decision for continuity. Speed comparisons use each link's `history` of three matching timestamped `{"at": 123, "mbps": 45}` samples. For preview, ordered profile `rules` each specify an `id`, allowed path IDs in `allow`, and optional `user`, `group`, `domain`, `cidr` or `protocol` matches. `require_domain` denies ambiguous or expired names. Supply `domains` and `dns_expires` in the session.

For provisioning, append `issue session.json credentials.json new-client.conf --api https://panel.example/api/v1 --token-file /private/admin-token --release deployed-release.json --client wg-ios-VERSION --config-schema wg-v1 --history /private/issued.sqlite` to `python3 contrib/deploy/scripts/topology.py /private/topology.json`. Bind each path to the specific published directed probe edge via a path field such as `"probe":{"source_node":"ENTRY-UUID","target_node":"EXIT-UUID","module":"tcp_connect","target":"exit.example:443"}`. Use a probe target appropriate to the path under test. Issuance fetches the published policy and the current fixed-query Prometheus observations through the admin API. Missing bindings, unapplied probe revisions, unknown health and samples older than 45 s exclude that path. For the speed strategy, bind the path to an enrolled `https_capacity` edge. The capacity recording rule produces `anet_link_capacity_mbps` from each successful fixed-size response. See the [measurement setup](../monitoring/README.md#controlled-https-transfer). The standard blackbox collector provides availability, loss and probe duration. Its HTTP body byte count does not measure path capacity. First provision each permitted direct or chained deployment with a distinct credential for the user. The private credentials map resolves profile ID then path ID to the matching client configuration filename relative to the credentials file. The command emits a JSON decision suitable for an operator audit log and writes the selected config with owner-only permissions. The client must import it to change routes. Destination and protocol rules support preview only. Issuance rejects profiles containing those rules. Previously issued credentials require explicit revocation at their gateway. Runtime flow selection and live migration are outside the scope of this command.

## Compatibility before upgrade

Keep the trusted manifest for the deployed server release with the private provisioning configuration. Each issued path must match an explicit client version, config schema and transport row in its verified client matrix. An empty matrix denies issuance. Use one persistent issuance ledger per deployment. It stores profile IDs and compatibility tuples. The ledger records a combination before writing its client credential, so a failed filesystem write can conservatively retain a combination.

Run `python3 contrib/deploy/scripts/release.py pre-upgrade candidate-release.json /private/issued.sqlite` before changing the server images. The JSON report includes each historically issued combination, the original server commit and the candidate compatibility decision. Exit code 2 means a combination lacks verification. Profiles issued outside the managed provisioning command require a separate inventory check.

## Manual incident reports

In the WebUI, open **Incident Reports** and enter the observation point, direction, transport and observed evidence or the result of an external check. The authenticated API records the operator ID and submission time in a dedicated database table. The page lists the latest 200 reports. Manual reports do not alter server probe observations or trigger automatic route selection.
