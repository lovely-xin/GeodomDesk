use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};

const MAX_REJECTED_PEERS: usize = 128;

#[derive(Clone, Copy)]
pub(crate) enum PermissionState {
    Permitted,
    Rejected,
}

/// Permissions belong to a TURN allocation and are keyed by peer IP, independently of port.
#[derive(Default)]
pub(crate) struct PermissionMap {
    states: HashMap<IpAddr, PermissionState>,
    rejected_peers: usize,
}

impl PermissionMap {
    pub(crate) fn state(&self, peer: IpAddr) -> Option<PermissionState> {
        self.states.get(&peer).copied()
    }

    pub(crate) fn permit(&mut self, peer: IpAddr) {
        self.states.insert(peer, PermissionState::Permitted);
    }

    pub(crate) fn reject(&mut self, peer: IpAddr) {
        if matches!(self.state(peer), Some(PermissionState::Rejected)) {
            return;
        }
        if self.rejected_peers == MAX_REJECTED_PEERS {
            self.states.remove(&peer);
            return;
        }
        self.states.insert(peer, PermissionState::Rejected);
        self.rejected_peers += 1;
    }

    pub(crate) fn addrs(&self) -> Vec<SocketAddr> {
        self.states
            .iter()
            .filter_map(|(&ip, state)| {
                matches!(state, PermissionState::Permitted).then_some(SocketAddr::new(ip, 0))
            })
            .collect()
    }
}
