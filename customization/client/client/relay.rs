use super::*;
use crate::common::get_next_nonkeyexchange_msg;

type RelayCandidate = (Stream, Option<KcpStream>, &'static str, bool);
const RELAY_PRECONNECT_IDLE: Duration = Duration::from_secs(5);
const RELAY_PRECONNECT_DELAY: Duration = Duration::from_millis(200);
const BROKER_PRECONNECT_IDLE: Duration = Duration::from_secs(3);
const STANDBY_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(3);

pub(super) enum DirectFailure {
    Transport(hbb_common::anyhow::Error),
    Handshake(hbb_common::anyhow::Error, &'static str, bool),
}

impl DirectFailure {
    pub(super) fn into_error(self) -> hbb_common::anyhow::Error {
        match self {
            Self::Transport(error) | Self::Handshake(error, _, _) => error,
        }
    }
}

impl Client {
    pub(super) async fn secure_relay_standby(
        peer: &str,
        signed_id: &[u8],
        key: &str,
        standby: Option<RelayCandidate>,
    ) -> Option<(RelayCandidate, Option<Vec<u8>>)> {
        let mut standby = standby?;
        let handshake = Self::secure_connection(peer, signed_id.to_vec(), key, &mut standby.0);
        match tokio::time::timeout(STANDBY_HANDSHAKE_TIMEOUT, handshake).await {
            Ok(Ok(peer_key)) => Some((standby, peer_key)),
            Ok(Err(error)) => {
                log::debug!("Ready relay handshake failed: {error}");
                None
            }
            Err(_) => {
                log::debug!("Ready relay did not finish its handshake");
                None
            }
        }
    }

    pub(super) async fn request_relay(
        peer: &str,
        relay_server: String,
        rendezvous_server: &str,
        secure: bool,
        key: &str,
        token: &str,
        conn_type: ConnType,
        switch_code: &str,
        mut prepared_broker: Option<Stream>,
    ) -> ResultType<Stream> {
        for attempt in 1..=3 {
            // Each signaling retry uses a fresh address and a fresh pairing ID.
            let mut socket = match prepared_broker.take() {
                Some(socket) => socket,
                None => {
                    Self::connect_relay_broker(
                        rendezvous_server,
                        key,
                        !token.is_empty() || !switch_code.is_empty(),
                    )
                    .await?
                }
            };
            let ipv4 = socket.local_addr().is_ipv4();
            let uuid = Uuid::new_v4().to_string();
            log::info!("#{attempt} request relay attempt, id: {peer}, uuid: {uuid}, relay_server: {relay_server}, secure: {secure}");
            let mut message = RendezvousMessage::new();
            message.set_request_relay(RequestRelay {
                id: peer.to_owned(),
                token: token.to_owned(),
                uuid: uuid.clone(),
                relay_server: relay_server.clone(),
                secure,
                switch_code: switch_code.to_owned(),
                ..Default::default()
            });
            let approval = async {
                socket.send(&message).await?;
                let Some(response) =
                    get_next_nonkeyexchange_msg(&mut socket, Some(CONNECT_TIMEOUT)).await
                else {
                    return Ok(None);
                };
                let Some(rendezvous_message::Union::RelayResponse(response)) = response.union
                else {
                    return Ok(None);
                };
                if !response.refuse_reason.is_empty() {
                    bail!(response.refuse_reason);
                }
                Ok(Some(()))
            };
            let prepare = Self::connect_relay_socket(&relay_server, ipv4);
            let Some(((), prepared)) =
                transport_race::prepare_while_authorizing(approval, prepare, RELAY_PRECONNECT_IDLE)
                    .await?
            else {
                continue;
            };
            // A failed or expired preconnection leaves the original dial path available.
            let conn = match prepared {
                Some(conn) => conn,
                None => Self::connect_relay_socket(&relay_server, ipv4).await?,
            };
            return Self::join_relay_socket(conn, peer, uuid, key, conn_type).await;
        }
        bail!("Timeout")
    }

    pub(super) async fn prepare_relay_broker_during<T>(
        direct: impl std::future::Future<Output = T>,
        server: &str,
        key: &str,
        needs_secure: bool,
    ) -> (T, Option<Stream>) {
        let prepare = async {
            tokio::time::sleep(RELAY_PRECONNECT_DELAY).await;
            Self::connect_relay_broker(server, key, needs_secure).await
        };
        transport_race::prepare_during(direct, prepare, BROKER_PRECONNECT_IDLE).await
    }

    async fn connect_relay_broker(
        server: &str,
        key: &str,
        needs_secure: bool,
    ) -> ResultType<Stream> {
        let mut socket = connect_tcp(server, CONNECT_TIMEOUT)
            .await
            .context("Failed to connect to rendezvous server")?;
        if !key.is_empty() && needs_secure {
            secure_tcp(&mut socket, key).await?;
        }
        Ok(socket)
    }

    pub(super) async fn create_relay(
        peer: &str,
        uuid: String,
        relay_server: String,
        key: &str,
        conn_type: ConnType,
        ipv4: bool,
    ) -> ResultType<Stream> {
        let conn = Self::connect_relay_socket(&relay_server, ipv4).await?;
        Self::join_relay_socket(conn, peer, uuid, key, conn_type).await
    }

    async fn connect_relay_socket(server: &str, ipv4: bool) -> ResultType<Stream> {
        let server = check_port(server, RELAY_PORT);
        // The proxy's address family does not constrain the relay target.
        let use_ws_proxy = Config::is_proxy() && is_ws_endpoint(&check_ws(&server));
        connect_tcp(ipv4_to_ipv6(server, ipv4 || use_ws_proxy), CONNECT_TIMEOUT)
            .await
            .context("Failed to connect to relay server")
    }

    async fn join_relay_socket(
        mut conn: Stream,
        peer: &str,
        uuid: String,
        key: &str,
        conn_type: ConnType,
    ) -> ResultType<Stream> {
        let mut message = RendezvousMessage::new();
        message.set_request_relay(RequestRelay {
            licence_key: key.to_owned(),
            id: peer.to_owned(),
            uuid,
            conn_type: conn_type.into(),
            ..Default::default()
        });
        conn.send(&message).await?;
        Ok(conn)
    }
}
