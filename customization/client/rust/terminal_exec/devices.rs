use super::{auth, cli, output, stage};
use hbb_common::{anyhow::Context, bail, config::AbPeer, tokio, ResultType};
use serde::Deserialize;
use serde_json::json;
use std::{collections::HashSet, time::Duration};

const PAGE_SIZE: usize = 100;
const MAX_PAGES: usize = 100;

#[derive(Deserialize)]
struct RecentPage {
    data: Vec<AbPeer>,
    total: usize,
}

#[derive(PartialEq, Eq, Debug)]
enum RecentResponse {
    Available,
    Unsupported,
    Unauthorized,
    Failed,
}

fn recent_status(status: u16) -> RecentResponse {
    match status {
        200 => RecentResponse::Available,
        404 | 405 | 501 => RecentResponse::Unsupported,
        401 | 403 => RecentResponse::Unauthorized,
        _ => RecentResponse::Failed,
    }
}

async fn cloud_recent(token: &str) -> ResultType<Option<Vec<AbPeer>>> {
    let mut rows = Vec::new();
    let mut seen = HashSet::new();
    for page in 1..=MAX_PAGES {
        let action = format!("recent/peers?current={page}&pageSize={PAGE_SIZE}");
        let (status, body) =
            crate::common::post_terminal_account(&action, "{}".to_owned(), Some(token)).await?;
        match recent_status(status) {
            RecentResponse::Available => {}
            RecentResponse::Unsupported if page == 1 => return Ok(None),
            RecentResponse::Unauthorized => {
                return Err(stage::wrap(
                    hbb_common::anyhow::anyhow!("GeodomDesk session expired or was rejected"),
                    "account_auth",
                    "not_started",
                ));
            }
            _ => bail!("Recent device service returned HTTP {status}"),
        }
        let response: RecentPage = serde_json::from_str(&body).context("Invalid recent device response")?;
        let count = response.data.len();
        if count > PAGE_SIZE || response.total > PAGE_SIZE * MAX_PAGES {
            bail!("Recent device service exceeded its pagination limit");
        }
        rows.extend(response.data.into_iter().filter(|peer| seen.insert(peer.id.clone())));
        if count < PAGE_SIZE || page * PAGE_SIZE >= response.total {
            return Ok(Some(rows));
        }
    }
    Ok(Some(rows))
}

async fn recent_devices(token: &str) -> ResultType<Vec<std::collections::HashMap<&'static str, String>>> {
    let local = match crate::geodomdesk_recent::cli_peers(token, None) {
        Some(peers) if !peers.is_empty() => return Ok(peers),
        local => local,
    };
    let cloud = tokio::time::timeout(Duration::from_secs(15), cloud_recent(token))
        .await
        .map_err(hbb_common::anyhow::Error::from)
        .and_then(|result| result)
        .map_err(|error| {
            if error.is::<stage::Stage>() {
                error
            } else {
                stage::wrap(error, "recent", "not_started")
            }
        })?;
    if let Some(cloud) = cloud {
        return Ok(crate::geodomdesk_recent::cli_cloud_peers(cloud));
    }
    if let Some(peers) = local {
        return Ok(peers);
    }
    let owner = verified_owner(token).await?;
    crate::geodomdesk_recent::cli_peers(token, owner.as_deref()).ok_or_else(|| {
        stage::wrap(
            hbb_common::anyhow::anyhow!(
                "This server does not support cloud recent devices. A matching desktop history is required."
            ),
            "recent",
            "not_started",
        )
    })
}

pub(super) async fn run(mut args: impl Iterator<Item = String>) -> ResultType<i32> {
    match args.next().as_deref() {
        Some("list" | "ls") => {}
        Some("--help" | "-h") => {
            println!("{}", cli::USAGE);
            return Ok(0);
        }
        _ => bail!("Use device list --recent [--online] [--json] [--pretty]"),
    }
    let mut online = false;
    let mut pretty = false;
    for arg in args {
        match arg.as_str() {
            "--recent" | "--json" | "--non-interactive" => {}
            "--online" => online = true,
            "--pretty" => pretty = true,
            "--help" | "-h" => {
                println!("{}", cli::USAGE);
                return Ok(0);
            }
            _ => bail!("Unknown device list option: {arg}"),
        }
    }
    let token = auth::account_token(None, true)
        .await
        .map_err(|error| stage::wrap(error, "account_auth", "not_started"))?;
    let peers = recent_devices(&token).await?;
    let mut online_ids = HashSet::new();
    if online && !peers.is_empty() {
        cli::sync_local_service_config().await;
        let ids: Vec<_> = peers.iter().filter_map(|peer| peer.get("id").cloned()).collect();
        // Batch the existing rendezvous query and bound the whole lookup.
        let lookup = tokio::time::timeout(Duration::from_secs(12), async {
            for batch in ids.chunks(128) {
                let (available, _) = crate::client::peer_online::query_cli_online_states(batch).await?;
                online_ids.extend(available);
            }
            Ok::<_, hbb_common::anyhow::Error>(())
        })
        .await
        .map_err(hbb_common::anyhow::Error::from)
        .and_then(|result| result)
        .map_err(|error| stage::wrap(error, "presence", "not_started"));
        lookup?;
    }
    let devices: Vec<_> = peers
        .into_iter()
        .filter_map(|peer| {
            let id = peer.get("id")?;
            let is_online = online.then(|| online_ids.contains(id));
            if is_online == Some(false) {
                return None;
            }
            Some(json!({
                "id": id,
                "alias": peer.get("alias"),
                "hostname": peer.get("hostname"),
                "platform": peer.get("platform"),
                "online": is_online,
            }))
        })
        .collect();
    let value = json!({"event": "device_list", "ok": true, "source": "recent", "devices": devices});
    if pretty {
        output::write_pretty(value)?;
    } else {
        output::write(value)?;
    }
    Ok(0)
}


#[cfg(feature = "embedded-ssh")]
async fn verified_owner(token: &str) -> ResultType<Option<String>> {
    let desktop_token = hbb_common::config::LocalConfig::get_option("access_token");
    if token == desktop_token || desktop_token.is_empty() {
        return Ok(None);
    }
    auth::verified_recent_owner(token).await.map(Some)
}

#[cfg(not(feature = "embedded-ssh"))]
async fn verified_owner(_token: &str) -> ResultType<Option<String>> {
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cloud_recent_wire_rows_respect_tombstones_and_expose_only_metadata() {
        let page: RecentPage = serde_json::from_value(json!({
            "total": 2,
            "data": [
                {"id": "123456781", "alias": "Workstation", "platform": "Windows",
                 "hash": "fixture-credential", "geodom_recent": "{\"timestamp\":2}"},
                {"id": "123456782", "geodom_recent": "{\"timestamp\":3,\"removed\":true}"}
            ]
        })).unwrap();
        let peers = crate::geodomdesk_recent::cli_cloud_peers(page.data);
        assert_eq!(peers.len(), 1);
        assert_eq!(peers[0]["id"], "123456781");
        assert_eq!(peers[0]["alias"], "Workstation");
        assert!(!peers[0].contains_key("hash"));
    }

    #[test]
    fn cloud_may_fall_back_only_when_the_server_lacks_the_endpoint() {
        assert_eq!(recent_status(200), RecentResponse::Available);
        for status in [404, 405, 501] {
            assert_eq!(recent_status(status), RecentResponse::Unsupported);
        }
        for status in [401, 403] {
            assert_eq!(recent_status(status), RecentResponse::Unauthorized);
        }
        for status in [302, 429, 500, 503] {
            assert_eq!(recent_status(status), RecentResponse::Failed);
        }
    }
}
