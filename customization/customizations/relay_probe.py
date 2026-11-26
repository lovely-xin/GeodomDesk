"""Wire periodic active relay RTT probing into desktop/service builds."""


def apply(changes):
    changes.replace(
        "src/lib.rs",
        "mod hbbs_http;",
        'mod hbbs_http;\n#[cfg(not(target_os = "ios"))]\npub(crate) mod geodomdesk_relay_probe;',
    )
    changes.replace(
        "src/common.rs",
        '        .map(|parsed| parsed.path().ends_with("/api/heartbeat"))',
        '        .map(|parsed| {\n'
        '            parsed.path().ends_with("/api/heartbeat")\n'
        '                || parsed.path().ends_with("/api/relay/probe")\n'
        '        })',
    )
    changes.replace(
        "src/rendezvous_mediator.rs",
        '        MANUAL_RESTART_NOTIFY.notify_waiters();\n        log::info!("server restart");',
        '        MANUAL_RESTART_NOTIFY.notify_waiters();\n'
        '        #[cfg(not(target_os = "ios"))]\n'
        '        crate::geodomdesk_relay_probe::notify_network_change();\n'
        '        log::info!("server restart");',
    )
    changes.replace(
        "src/rendezvous_mediator.rs",
        "        crate::test_nat_type();\n        if config::is_outgoing_only() {",
        "        crate::test_nat_type();\n"
        '        #[cfg(not(target_os = "ios"))]\n'
        "        crate::geodomdesk_relay_probe::start();\n"
        "        if config::is_outgoing_only() {",
    )
    changes.replace(
        "src/rendezvous_mediator.rs",
        "                    Ok(register_pk_response::Result::OK) => {\n                        Config::set_key_confirmed(true);",
        "                    Ok(register_pk_response::Result::OK) => {\n"
        "                        Config::set_key_confirmed(true);\n"
        '                        #[cfg(not(target_os = "ios"))]\n'
        "                        crate::geodomdesk_relay_probe::notify_network_change();",
    )
