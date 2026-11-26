#[cfg(not(target_os = "ios"))]
mod logic;

#[cfg(not(target_os = "ios"))]
mod imp {
    use super::logic::{
        build_heartbeat_probe_payload, build_probe_payload, cap_candidates, clamp_probe_interval,
        filter_reportable_failures, interleave_addrs, jittered_secs, next_backoff_secs,
        normalize_probe_target, parse_heartbeat_probes, parse_probe_response,
    };
    use crate::common::API_LOG_INTERVAL;
    use hbb_common::{
        config::{self, Config, RELAY_PORT},
        log,
        tokio::{
            self,
            sync::{mpsc, Semaphore},
        },
    };
    use std::{
        collections::HashMap,
        net::SocketAddr,
        sync::{Arc, OnceLock},
        time::{Duration, Instant},
    };

    const INITIAL_PROBE_DELAY: Duration = Duration::from_secs(5);
    const DEFAULT_PROBE_INTERVAL_SECS: u64 = 120;
    const FAILURE_WAKE_DEBOUNCE: Duration = Duration::from_secs(15);
    const NETWORK_WAKE_DEBOUNCE: Duration = Duration::from_secs(5);
    const PROBE_DNS_TIMEOUT: Duration = Duration::from_millis(1500);
    const PROBE_TCP_TIMEOUT: Duration = Duration::from_millis(1500);
    const CONFIRM_DELAY: Duration = Duration::from_millis(100);
    const HIGH_RTT_CONFIRM_MS: f64 = 200.0;
    const MAX_PROBE_CANDIDATES: usize = 16;
    const MAX_CONCURRENT_PROBES: usize = 6;

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum WakeReason {
        FailureHint,
        NetworkChange,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum ProbeMode {
        DirectEndpoint,
        HeartbeatFallback,
    }

    static STARTED: OnceLock<()> = OnceLock::new();
    static WAKE_TX: OnceLock<mpsc::Sender<WakeReason>> = OnceLock::new();

    pub(crate) fn start() {
        STARTED.get_or_init(|| {
            let (tx, rx) = mpsc::channel::<WakeReason>(8);
            let _ = WAKE_TX.set(tx);
            if let Err(err) = std::thread::Builder::new()
                .name("relay-probe".to_owned())
                .spawn(move || run_loop(rx))
            {
                log::debug!("Failed to spawn relay-probe thread: {err}");
            }
        });
    }

    pub(crate) fn report_relay_failure(relay_server: &str) {
        if relay_server.trim().is_empty() {
            return;
        }
        start();
        if let Some(tx) = WAKE_TX.get() {
            let _ = tx.try_send(WakeReason::FailureHint);
        }
    }

    pub(crate) fn notify_network_change() {
        if let Some(tx) = WAKE_TX.get() {
            let _ = tx.try_send(WakeReason::NetworkChange);
        }
    }

    #[tokio::main(flavor = "current_thread")]
    async fn run_loop(mut wake_rx: mpsc::Receiver<WakeReason>) {
        let mut interval_secs = DEFAULT_PROBE_INTERVAL_SECS;
        let mut backoff_secs: Option<u64> = None;
        let mut last_failure_wake: Option<Instant> = None;
        let mut last_network_wake: Option<Instant> = None;
        let mut last_probe_at: Option<Instant> = None;
        let mut known_ver: u64 = 0;
        let mut cached_servers: Vec<String> = Vec::new();
        let mut mode = ProbeMode::DirectEndpoint;
        let mut round: u64 = 0;
        let mut next_probe_at = Instant::now() + INITIAL_PROBE_DELAY;

        loop {
            let sleep_dur = next_probe_at.saturating_duration_since(Instant::now());
            let mut wake_reason: Option<WakeReason> = None;
            if !sleep_dur.is_zero() {
                tokio::select! {
                    _ = tokio::time::sleep(sleep_dur) => {}
                    maybe_reason = wake_rx.recv() => {
                        let Some(reason) = maybe_reason else { return; };
                        match reason {
                            WakeReason::FailureHint => {
                                if last_failure_wake
                                    .is_some_and(|at| at.elapsed() < FAILURE_WAKE_DEBOUNCE)
                                {
                                    continue;
                                }
                                last_failure_wake = Some(Instant::now());
                                wake_reason = Some(WakeReason::FailureHint);
                            }
                            WakeReason::NetworkChange => {
                                if last_network_wake
                                    .is_some_and(|at| at.elapsed() < NETWORK_WAKE_DEBOUNCE)
                                {
                                    continue;
                                }
                                last_network_wake = Some(Instant::now());
                                backoff_secs = None;
                                if known_ver > 0
                                    && last_probe_at.is_some_and(|at| {
                                        at.elapsed()
                                            < Duration::from_secs(
                                                super::logic::MIN_PROBE_INTERVAL_SECS,
                                            )
                                    })
                                {
                                    continue;
                                }
                                mode = ProbeMode::DirectEndpoint;
                                wake_reason = Some(WakeReason::NetworkChange);
                            }
                        }
                    }
                }
            }

            round = round.saturating_add(1);
            let id = Config::get_id();
            if !should_run_probe() || id.is_empty() {
                let wait = jittered_secs(interval_secs, &id, round);
                next_probe_at = Instant::now() + Duration::from_secs(wait);
                continue;
            }
            let Some((direct_url, hb_url)) = api_urls() else {
                let wait = jittered_secs(interval_secs, &id, round);
                next_probe_at = Instant::now() + Duration::from_secs(wait);
                continue;
            };
            let uuid = crate::encode64(hbb_common::get_uuid());
            if uuid.is_empty() {
                let wait = jittered_secs(interval_secs, &id, round);
                next_probe_at = Instant::now() + Duration::from_secs(wait);
                continue;
            }

            let pre_measurements = if !cached_servers.is_empty()
                && wake_reason != Some(WakeReason::NetworkChange)
            {
                let cands = cap_candidates(&cached_servers, MAX_PROBE_CANDIDATES);
                if cands.len() > 1 {
                    let (rtt_map, raw_failed) = probe_candidates(&cands).await;
                    let reportable_failed = filter_reportable_failures(&rtt_map, raw_failed);
                    Some((rtt_map, reportable_failed))
                } else {
                    None
                }
            } else {
                None
            };

            let meas_ref = pre_measurements
                .as_ref()
                .map(|(rtt, failed)| (rtt, failed.as_slice()));

            let outcome = match mode {
                ProbeMode::DirectEndpoint => {
                    let payload = build_probe_payload(&id, &uuid, known_ver, meas_ref);
                    match crate::post_request_with_status(direct_url.clone(), payload, "").await {
                        Ok((404 | 405, _)) => {
                            mode = ProbeMode::HeartbeatFallback;
                            fetch_via_heartbeat(
                                &hb_url,
                                &id,
                                &uuid,
                                known_ver,
                                meas_ref,
                                &mut backoff_secs,
                            )
                            .await
                        }
                        Ok((200, body)) => match parse_probe_response(&body) {
                            Some(resp) => {
                                backoff_secs = None;
                                Some((resp.ver, Some(resp.servers), resp.probe_interval_secs))
                            }
                            None => {
                                backoff_secs = Some(next_backoff_secs(backoff_secs));
                                None
                            }
                        },
                        Ok((status, _)) => {
                            let b = next_backoff_secs(backoff_secs);
                            backoff_secs = Some(b);
                            hbb_common::throttled_log!(
                                API_LOG_INTERVAL,
                                debug,
                                "Relay probe returned HTTP {}; retrying in {}s",
                                status,
                                b
                            );
                            None
                        }
                        Err(_) => {
                            let b = next_backoff_secs(backoff_secs);
                            backoff_secs = Some(b);
                            hbb_common::throttled_log!(
                                API_LOG_INTERVAL,
                                debug,
                                "Relay probe endpoint unavailable; backing off {}s",
                                b
                            );
                            None
                        }
                    }
                }
                ProbeMode::HeartbeatFallback => {
                    fetch_via_heartbeat(
                        &hb_url,
                        &id,
                        &uuid,
                        known_ver,
                        meas_ref,
                        &mut backoff_secs,
                    )
                    .await
                }
            };

            if let Some((ver, maybe_servers, maybe_interval)) = outcome {
                let version_changed = ver != known_ver;
                known_ver = ver;
                if let Some(servers) = maybe_servers {
                    cached_servers = servers;
                }
                if let Some(next_int) = maybe_interval {
                    interval_secs = clamp_probe_interval(next_int);
                }
                last_probe_at = Some(Instant::now());

                if pre_measurements.is_none() || version_changed {
                    let cands = cap_candidates(&cached_servers, MAX_PROBE_CANDIDATES);
                    if cands.len() > 1 {
                        let (rtt_map, raw_failed) = probe_candidates(&cands).await;
                        let reportable_failed = filter_reportable_failures(&rtt_map, raw_failed);
                        if !rtt_map.is_empty() {
                            match mode {
                                ProbeMode::DirectEndpoint => {
                                    let p = build_probe_payload(
                                        &id,
                                        &uuid,
                                        known_ver,
                                        Some((&rtt_map, &reportable_failed)),
                                    );
                                    let _ = crate::post_request_with_status(direct_url, p, "").await;
                                }
                                ProbeMode::HeartbeatFallback => {
                                    let p = build_heartbeat_probe_payload(
                                        &id,
                                        &uuid,
                                        known_ver,
                                        Some((&rtt_map, &reportable_failed)),
                                    );
                                    let _ = crate::post_request_with_status(hb_url, p, "").await;
                                }
                            }
                        }
                    }
                }
            }

            let wait_secs = backoff_secs
                .unwrap_or_else(|| jittered_secs(clamp_probe_interval(interval_secs), &id, round));
            next_probe_at = Instant::now() + Duration::from_secs(wait_secs);
        }
    }

    async fn fetch_via_heartbeat(
        hb_url: &str,
        id: &str,
        uuid: &str,
        known_ver: u64,
        measurements: Option<(&HashMap<String, f64>, &[String])>,
        backoff_secs: &mut Option<u64>,
    ) -> Option<(u64, Option<Vec<String>>, Option<u64>)> {
        let payload = build_heartbeat_probe_payload(id, uuid, known_ver, measurements);
        match crate::post_request_with_status(hb_url.to_owned(), payload, "").await {
            Ok((200, body)) => match parse_heartbeat_probes(&body) {
                Some(hp) => {
                    *backoff_secs = None;
                    Some((hp.ver, hp.servers, hp.probe_interval_secs))
                }
                None => {
                    *backoff_secs = Some(next_backoff_secs(*backoff_secs));
                    None
                }
            },
            _ => {
                *backoff_secs = Some(next_backoff_secs(*backoff_secs));
                None
            }
        }
    }

    fn should_run_probe() -> bool {
        !config::option2bool("stop-service", &Config::get_option("stop-service"))
            && !Config::is_proxy()
            && !config::use_ws()
            && !Config::get_id().is_empty()
    }

    fn api_urls() -> Option<(String, String)> {
        let base = crate::common::get_api_server(
            Config::get_option("api-server"),
            Config::get_option("custom-rendezvous-server"),
        );
        if base.is_empty() || crate::is_public(&base) {
            return None;
        }
        let trimmed = base.trim_end_matches('/');
        Some((
            format!("{trimmed}/api/relay/probe"),
            format!("{trimmed}/api/heartbeat"),
        ))
    }

    async fn probe_candidates(candidates: &[String]) -> (HashMap<String, f64>, Vec<String>) {
        let sem = Arc::new(Semaphore::new(MAX_CONCURRENT_PROBES));
        let mut handles = Vec::with_capacity(candidates.len());
        for raw in candidates {
            let endpoint = raw.clone();
            let permit_pool = sem.clone();
            handles.push(tokio::spawn(async move {
                let Ok(_permit) = permit_pool.acquire_owned().await else {
                    return (endpoint, None);
                };
                let target = normalize_probe_target(&endpoint, RELAY_PORT as u16);
                let measured = probe_tcp_target(&target).await;
                (endpoint, measured)
            }));
        }
        let mut rtt = HashMap::new();
        let mut failed = Vec::new();
        for handle in handles {
            if let Ok((endpoint, measured)) = handle.await {
                match measured {
                    Some(ms) => {
                        rtt.insert(endpoint, ms);
                    }
                    None => failed.push(endpoint),
                }
            }
        }
        (rtt, failed)
    }

    async fn single_tcp_sample(addrs: &[SocketAddr]) -> Option<f64> {
        if addrs.is_empty() {
            return None;
        }
        let budget = PROBE_TCP_TIMEOUT;
        let per_addr = budget
            .checked_div(addrs.len() as u32)
            .unwrap_or(budget)
            .max(Duration::from_millis(400));
        let mut remaining = budget;
        for &addr in addrs {
            if remaining.is_zero() {
                break;
            }
            let step = per_addr.min(remaining);
            let started = Instant::now();
            let res = tokio::time::timeout(step, tokio::net::TcpStream::connect(addr)).await;
            let elapsed = started.elapsed();
            if let Ok(Ok(stream)) = res {
                drop(stream);
                let ms = (elapsed.as_secs_f64() * 1000.0).max(0.1);
                return Some((ms * 10.0).round() / 10.0);
            }
            remaining = remaining.saturating_sub(elapsed);
        }
        None
    }

    async fn probe_tcp_target(target: &str) -> Option<f64> {
        let addrs: Vec<SocketAddr> = if let Ok(sa) = target.parse::<SocketAddr>() {
            vec![sa]
        } else {
            let Ok(Ok(resolved)) =
                tokio::time::timeout(PROBE_DNS_TIMEOUT, tokio::net::lookup_host(target)).await
            else {
                return None;
            };
            interleave_addrs(resolved, 4)
        };
        if addrs.is_empty() {
            return None;
        }
        let first = single_tcp_sample(&addrs).await;
        match first {
            Some(ms) if ms <= HIGH_RTT_CONFIRM_MS => Some(ms),
            maybe_first => {
                tokio::time::sleep(CONFIRM_DELAY).await;
                let second = single_tcp_sample(&addrs).await;
                match (maybe_first, second) {
                    (Some(a), Some(b)) => Some(a.min(b)),
                    (Some(a), None) => Some(a),
                    (None, Some(b)) => Some(b),
                    (None, None) => None,
                }
            }
        }
    }
}

#[cfg(not(target_os = "ios"))]
pub(crate) use imp::{notify_network_change, report_relay_failure, start};
