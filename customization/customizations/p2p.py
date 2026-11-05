"""Keep UDP punching independent of the rendezvous TCP NAT classification."""


def apply(changes):
    path = "src/rendezvous_mediator.rs"
    changes.replace(
        path,
        "    async fn handle_punch_hole(&self, ph: PunchHole, server: ServerPtr) -> ResultType<()> {",
        """    fn should_relay_ipv4(
        forced: bool,
        udp_port: i32,
        tcp_symmetric: bool,
        tcp_disabled: bool,
    ) -> bool {
        // The rendezvous NAT test classifies TCP, not the independently observed UDP mapping.
        forced || (udp_port <= 0 && (tcp_symmetric || tcp_disabled))
    }

    async fn handle_punch_hole(&self, ph: PunchHole, server: ServerPtr) -> ResultType<()> {""",
    )
    changes.replace(
        path,
        "// A symmetric NAT relays the legacy transports but deliberately not WebRTC:",
        "// A symmetric TCP NAT relays the legacy TCP leg but deliberately not WebRTC:",
    )
    changes.replace(
        path,
        """        let relay_v4 = ph.nat_type.enum_value() == Ok(NatType::SYMMETRIC)
            || Config::get_nat_type() == NatType::SYMMETRIC as i32
            || relay
            || (config::is_disable_tcp_listen() && ph.udp_port <= 0);""",
        """        let relay_v4 = Self::should_relay_ipv4(
            relay,
            ph.udp_port,
            ph.nat_type.enum_value() == Ok(NatType::SYMMETRIC)
                || Config::get_nat_type() == NatType::SYMMETRIC as i32,
            config::is_disable_tcp_listen(),
        );""",
    )
    changes.replace(
        path,
        "    #[tokio::test]\n    async fn manual_restart_interrupts_retry_wait() {",
        """    #[test]
    fn udp_mapping_is_not_disabled_by_tcp_nat_test() {
        assert!(!RendezvousMediator::should_relay_ipv4(false, 9080, true, false));
        assert!(!RendezvousMediator::should_relay_ipv4(false, 9080, true, true));
        assert!(RendezvousMediator::should_relay_ipv4(true, 9080, false, false));
        assert!(RendezvousMediator::should_relay_ipv4(false, 0, true, false));
        assert!(RendezvousMediator::should_relay_ipv4(false, 0, false, true));
        assert!(!RendezvousMediator::should_relay_ipv4(false, 0, false, false));
    }

    #[tokio::test]
    async fn manual_restart_interrupts_retry_wait() {""",
    )
