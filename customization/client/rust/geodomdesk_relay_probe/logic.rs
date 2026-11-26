use serde_derive::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    net::{IpAddr, SocketAddr},
};

pub(crate) const MIN_PROBE_INTERVAL_SECS: u64 = 30;
pub(crate) const MAX_PROBE_INTERVAL_SECS: u64 = 600;
pub(crate) const MIN_BACKOFF_SECS: u64 = 15;
pub(crate) const MAX_BACKOFF_SECS: u64 = 600;

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub(crate) struct ProbeServerResponse {
    pub ver: u64,
    pub servers: Vec<String>,
    #[serde(default)]
    pub probe_interval_secs: Option<u64>,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub(crate) struct HeartbeatProbeUpdate {
    pub ver: u64,
    #[serde(default)]
    pub servers: Option<Vec<String>>,
    #[serde(default)]
    pub probe_interval_secs: Option<u64>,
}

pub(crate) fn clamp_probe_interval(secs: u64) -> u64 {
    secs.clamp(MIN_PROBE_INTERVAL_SECS, MAX_PROBE_INTERVAL_SECS)
}

pub(crate) fn next_backoff_secs(current: Option<u64>) -> u64 {
    match current {
        None => MIN_BACKOFF_SECS,
        Some(secs) => secs.saturating_mul(2).clamp(MIN_BACKOFF_SECS, MAX_BACKOFF_SECS),
    }
}

pub(crate) fn jittered_secs(base_secs: u64, seed: &str, round: u64) -> u64 {
    if base_secs <= 1 {
        return base_secs;
    }
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325 ^ round.wrapping_mul(0x9e37_79b9_7f4a_7c15);
    for &byte in seed.as_bytes() {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x1000_0000_01b3);
    }
    let span = (base_secs / 5).max(1); // +/- 10%
    let offset = hash % (span + 1);
    base_secs
        .saturating_sub(span / 2)
        .saturating_add(offset)
        .max(1)
}

pub(crate) fn parse_probe_response(raw: &str) -> Option<ProbeServerResponse> {
    let val: Value = serde_json::from_str(raw).ok()?;
    let obj = val.as_object()?;
    if obj.contains_key("error") {
        return None;
    }
    serde_json::from_value::<ProbeServerResponse>(val).ok()
}

pub(crate) fn parse_heartbeat_probes(raw: &str) -> Option<HeartbeatProbeUpdate> {
    let val: Value = serde_json::from_str(raw).ok()?;
    let obj = val.as_object()?;
    if obj.contains_key("error") {
        return None;
    }
    let probes = obj.get("relay_probes")?;
    if !probes.is_object() {
        return None;
    }
    serde_json::from_value::<HeartbeatProbeUpdate>(probes.clone()).ok()
}

pub(crate) fn cap_candidates(servers: &[String], limit: usize) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for raw in servers {
        let s = raw.trim();
        if s.is_empty() || !seen.insert(s.to_owned()) {
            continue;
        }
        out.push(s.to_owned());
        if out.len() >= limit {
            break;
        }
    }
    out
}

pub(crate) fn normalize_probe_target(server: &str, default_port: u16) -> String {
    let trimmed = server.trim();
    if let Ok(sa) = trimmed.parse::<SocketAddr>() {
        return sa.to_string();
    }
    if let Ok(ip) = trimmed.parse::<IpAddr>() {
        return match ip {
            IpAddr::V4(v4) => format!("{v4}:{default_port}"),
            IpAddr::V6(v6) => format!("[{v6}]:{default_port}"),
        };
    }
    if let Some(rest) = trimmed.strip_prefix('[') {
        if let Some((host, after)) = rest.split_once(']') {
            if let Ok(ip) = host.parse::<IpAddr>() {
                let port = after
                    .strip_prefix(':')
                    .and_then(|p| p.parse::<u16>().ok())
                    .unwrap_or(default_port);
                return match ip {
                    IpAddr::V4(v4) => format!("{v4}:{port}"),
                    IpAddr::V6(v6) => format!("[{v6}]:{port}"),
                };
            }
        }
    }
    if let Some((host, port)) = trimmed.rsplit_once(':') {
        if !host.is_empty() && !host.contains(':') && port.parse::<u16>().is_ok() {
            return trimmed.to_owned();
        }
    }
    format!("{trimmed}:{default_port}")
}

pub(crate) fn interleave_addrs(
    addrs: impl IntoIterator<Item = SocketAddr>,
    limit: usize,
) -> Vec<SocketAddr> {
    let mut v4 = Vec::new();
    let mut v6 = Vec::new();
    for sa in addrs {
        if sa.is_ipv4() {
            v4.push(sa);
        } else {
            v6.push(sa);
        }
    }
    let mut out = Vec::with_capacity(limit.min(v4.len() + v6.len()));
    let max_len = v4.len().max(v6.len());
    for i in 0..max_len {
        if let Some(a) = v6.get(i) {
            out.push(*a);
            if out.len() >= limit {
                break;
            }
        }
        if let Some(a) = v4.get(i) {
            out.push(*a);
            if out.len() >= limit {
                break;
            }
        }
    }
    out
}

pub(crate) fn filter_reportable_failures(
    rtt_map: &HashMap<String, f64>,
    failed: Vec<String>,
) -> Vec<String> {
    if rtt_map.is_empty() {
        Vec::new()
    } else {
        failed
    }
}

pub(crate) fn build_probe_payload(
    id: &str,
    uuid: &str,
    ver: u64,
    measurements: Option<(&HashMap<String, f64>, &[String])>,
) -> String {
    let mut obj = json!({
        "id": id,
        "uuid": uuid,
        "ver": ver,
    });
    if let Some((rtt, failed)) = measurements {
        if let Some(map) = obj.as_object_mut() {
            if !rtt.is_empty() {
                map.insert("rtt".to_owned(), json!(rtt));
                if !failed.is_empty() {
                    map.insert("failed".to_owned(), json!(failed));
                }
            }
        }
    }
    obj.to_string()
}

pub(crate) fn build_heartbeat_probe_payload(
    id: &str,
    uuid: &str,
    relay_probe_ver: u64,
    measurements: Option<(&HashMap<String, f64>, &[String])>,
) -> String {
    let mut obj = json!({
        "id": id,
        "uuid": uuid,
        "relay_probe_ver": relay_probe_ver,
    });
    if let Some((rtt, failed)) = measurements {
        if let Some(map) = obj.as_object_mut() {
            if !rtt.is_empty() {
                map.insert("relay_rtt".to_owned(), json!(rtt));
                if !failed.is_empty() {
                    map.insert("relay_failed".to_owned(), json!(failed));
                }
            }
        }
    }
    obj.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_probe_targets_and_caps_candidates() {
        assert_eq!(normalize_probe_target("1.2.3.4", 21117), "1.2.3.4:21117");
        assert_eq!(
            normalize_probe_target("1.2.3.4:21119", 21117),
            "1.2.3.4:21119"
        );
        assert_eq!(
            normalize_probe_target("2001:db8::1", 21117),
            "[2001:db8::1]:21117"
        );
        assert_eq!(
            normalize_probe_target("[2001:db8::1]", 21117),
            "[2001:db8::1]:21117"
        );
        assert_eq!(
            normalize_probe_target("[2001:db8::1]:21119", 21117),
            "[2001:db8::1]:21119"
        );
        assert_eq!(
            normalize_probe_target("relay.example.com", 21117),
            "relay.example.com:21117"
        );

        let raw: Vec<String> = vec![
            "r1:21117".into(),
            "".into(),
            "r1:21117".into(),
            "r2:21117".into(),
            "r3:21117".into(),
        ];
        assert_eq!(cap_candidates(&raw, 2), vec!["r1:21117", "r2:21117"]);
    }

    #[test]
    fn parses_strict_responses_and_rejects_error_payloads() {
        assert!(parse_probe_response(r#"{"error":"Access denied"}"#).is_none());
        assert!(parse_probe_response(r#"{"ver":1}"#).is_none());
        assert!(parse_probe_response(r#"{"servers":["r1"]}"#).is_none());
        assert!(parse_probe_response(r#"{"ver":1,"servers":["r1"],"error":"stale"}"#).is_none());

        let parsed = parse_probe_response(
            r#"{"ver":4,"servers":["r1:21117","r2:21117"],"probe_interval_secs":90}"#,
        )
        .unwrap();
        assert_eq!(parsed.ver, 4);
        assert_eq!(parsed.servers, vec!["r1:21117", "r2:21117"]);
        assert_eq!(clamp_probe_interval(10), 30);
        assert_eq!(clamp_probe_interval(9999), 600);

        let hb_full = parse_heartbeat_probes(
            r#"{"sysinfo":true,"relay_probes":{"ver":5,"servers":["r1:21117"],"probe_interval_secs":45}}"#,
        )
        .unwrap();
        assert_eq!(hb_full.ver, 5);
        assert_eq!(hb_full.servers.as_deref(), Some(&["r1:21117".to_owned()][..]));

        let hb_omitted = parse_heartbeat_probes(
            r#"{"relay_probes":{"ver":5,"probe_interval_secs":45}}"#,
        )
        .unwrap();
        assert_eq!(hb_omitted.ver, 5);
        assert!(hb_omitted.servers.is_none());
        assert!(parse_heartbeat_probes(r#"{}"#).is_none());
        assert!(parse_heartbeat_probes(r#"{"error":"Access denied","relay_probes":{"ver":1}}"#).is_none());
    }

    #[test]
    fn anti_poisoning_backoff_jitter_and_interleave() {
        let empty_rtt = HashMap::new();
        assert!(filter_reportable_failures(&empty_rtt, vec!["r1".to_owned()]).is_empty());

        let valid_rtt = HashMap::from([("r1:21117".to_owned(), 12.5)]);
        let failed = filter_reportable_failures(&valid_rtt, vec!["r2:21117".to_owned()]);
        assert_eq!(failed, vec!["r2:21117"]);

        let probe_json = build_probe_payload("dev1", "uuid64", 4, Some((&valid_rtt, &failed)));
        let pv: Value = serde_json::from_str(&probe_json).unwrap();
        assert_eq!(pv["ver"], 4);
        assert_eq!(pv["rtt"]["r1:21117"], 12.5);
        assert_eq!(pv["failed"][0], "r2:21117");

        // When rtt is empty, failed is never serialized even if passed
        let safe_empty = build_probe_payload("dev1", "uuid64", 4, Some((&empty_rtt, &failed)));
        let sv: Value = serde_json::from_str(&safe_empty).unwrap();
        assert!(sv.get("failed").is_none());

        let hb_json =
            build_heartbeat_probe_payload("dev1", "uuid64", 4, Some((&valid_rtt, &failed)));
        let hv: Value = serde_json::from_str(&hb_json).unwrap();
        assert_eq!(hv["relay_probe_ver"], 4);
        assert_eq!(hv["relay_rtt"]["r1:21117"], 12.5);
        assert_eq!(hv["relay_failed"][0], "r2:21117");

        assert_eq!(next_backoff_secs(None), 15);
        assert_eq!(next_backoff_secs(Some(15)), 30);
        assert_eq!(next_backoff_secs(Some(300)), 600);
        assert_eq!(next_backoff_secs(Some(600)), 600);

        let j = jittered_secs(120, "dev1", 7);
        assert!((108..=132).contains(&j), "j={j}");

        let addrs: Vec<SocketAddr> = vec![
            "1.1.1.1:21117".parse().unwrap(),
            "2.2.2.2:21117".parse().unwrap(),
            "[2001:db8::1]:21117".parse().unwrap(),
            "[2001:db8::2]:21117".parse().unwrap(),
        ];
        let interleaved = interleave_addrs(addrs, 4);
        assert_eq!(
            interleaved,
            vec![
                "[2001:db8::1]:21117".parse().unwrap(),
                "1.1.1.1:21117".parse().unwrap(),
                "[2001:db8::2]:21117".parse().unwrap(),
                "2.2.2.2:21117".parse().unwrap(),
            ]
        );
    }
}
