use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::str::FromStr;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use portable_atomic::AtomicU16;
use stun::message::*;
use tokio::sync::mpsc;
use tokio::time::Duration;
use util::sync::Mutex;
use util::Conn;

use crate::error::*;

const MAX_RTX_INTERVAL_IN_MS: u16 = 1600;
const MAX_RTX_COUNT: u16 = 7; // total 7 requests (Rc)

async fn on_rtx_timeout(
    conn: &Arc<dyn Conn + Send + Sync>,
    tr_map: &Arc<Mutex<TransactionMap>>,
    tr_key: &str,
    n_rtx: u16,
) -> bool {
    if n_rtx == MAX_RTX_COUNT {
        finish_failed(tr_map, tr_key).await;
        return true;
    }

    let payload = tr_map
        .lock()
        .find(tr_key)
        .map(|tr| (tr.raw.clone(), tr.to.clone()));
    let Some((tr_raw, tr_to)) = payload else {
        return true;
    };

    log::trace!(
        "retransmitting transaction {} to {} (n_rtx={})",
        tr_key,
        tr_to,
        n_rtx
    );

    let sent = match SocketAddr::from_str(&tr_to) {
        Ok(dst) => conn.send_to(&tr_raw, dst).await.is_ok(),
        Err(_) => false,
    };
    if !sent {
        finish_failed(tr_map, tr_key).await;
    }
    !sent
}

async fn finish_failed(tr_map: &Mutex<TransactionMap>, tr_key: &str) {
    let transaction = tr_map.lock().delete(tr_key);
    if let Some(tr) = transaction {
        if !tr
            .write_result(TransactionResult {
                err: Some(Error::Other(format!(
                    "{:?} {}",
                    Error::ErrAllRetransmissionsFailed,
                    tr_key
                ))),
                ..Default::default()
            })
            .await
        {
            log::debug!("no listener for transaction");
        }
    }
}

/// `TransactionResult` is a bag of result values of a transaction.
#[derive(Debug)] //Clone
pub struct TransactionResult {
    pub msg: Message,
    pub from: SocketAddr,
    pub retries: u16,
    pub err: Option<Error>,
}

impl Default for TransactionResult {
    fn default() -> Self {
        TransactionResult {
            msg: Message::default(),
            from: SocketAddr::new(IpAddr::V4(Ipv4Addr::new(0, 0, 0, 0)), 0),
            retries: 0,
            err: None,
        }
    }
}

/// `TransactionConfig` is a set of config params used by [`Transaction::new()`].
#[derive(Default)]
pub struct TransactionConfig {
    pub key: String,
    pub raw: Vec<u8>,
    pub to: String,
    pub interval: u16,
    pub ignore_result: bool, // true to throw away the result of this transaction (it will not be readable using wait_for_result)
}

/// `Transaction` represents a transaction.
#[derive(Debug)]
pub struct Transaction {
    pub key: String,
    pub raw: Vec<u8>,
    pub to: String,
    pub n_rtx: Arc<AtomicU16>,
    pub interval: Arc<AtomicU16>,
    timer_ch_tx: Option<mpsc::Sender<()>>,
    result_ch_tx: Option<mpsc::Sender<TransactionResult>>,
    result_ch_rx: Option<mpsc::Receiver<TransactionResult>>,
}

impl Default for Transaction {
    fn default() -> Self {
        Transaction {
            key: String::new(),
            raw: vec![],
            to: String::new(),
            n_rtx: Arc::new(AtomicU16::new(0)),
            interval: Arc::new(AtomicU16::new(0)),
            //timer: None,
            timer_ch_tx: None,
            result_ch_tx: None,
            result_ch_rx: None,
        }
    }
}

impl Transaction {
    /// Creates a new [`Transaction`] using the given `config`.
    pub fn new(config: TransactionConfig) -> Self {
        let (result_ch_tx, result_ch_rx) = if !config.ignore_result {
            let (tx, rx) = mpsc::channel(1);
            (Some(tx), Some(rx))
        } else {
            (None, None)
        };

        Transaction {
            key: config.key,
            raw: config.raw,
            to: config.to,
            interval: Arc::new(AtomicU16::new(config.interval)),
            result_ch_tx,
            result_ch_rx,
            ..Default::default()
        }
    }

    /// Starts the transaction timer.
    pub fn start_rtx_timer(
        &mut self,
        conn: Arc<dyn Conn + Send + Sync>,
        tr_map: Arc<Mutex<TransactionMap>>,
    ) {
        let (timer_ch_tx, mut timer_ch_rx) = mpsc::channel(1);
        self.timer_ch_tx = Some(timer_ch_tx);
        let (n_rtx, interval, key) = (self.n_rtx.clone(), self.interval.clone(), self.key.clone());

        tokio::spawn(async move {
            let retransmit = async {
                loop {
                    let wait = interval.load(Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(wait.into())).await;
                    let rtx = n_rtx.fetch_add(1, Ordering::SeqCst) + 1;
                    interval.store(
                        wait.saturating_mul(2).min(MAX_RTX_INTERVAL_IN_MS),
                        Ordering::SeqCst,
                    );
                    if on_rtx_timeout(&conn, &tr_map, &key, rtx).await {
                        return;
                    }
                }
            };
            tokio::select! {
                biased;
                _ = timer_ch_rx.recv() => {}
                _ = retransmit => {}
            }
        });
    }

    /// Stops the transaction timer.
    pub fn stop_rtx_timer(&mut self) {
        if self.timer_ch_tx.is_some() {
            self.timer_ch_tx.take();
        }
    }

    /// Writes the result to the result channel.
    pub async fn write_result(&self, res: TransactionResult) -> bool {
        if let Some(result_ch) = &self.result_ch_tx {
            result_ch.send(res).await.is_ok()
        } else {
            false
        }
    }

    /// Returns the result channel.
    pub fn get_result_channel(&mut self) -> Option<mpsc::Receiver<TransactionResult>> {
        self.result_ch_rx.take()
    }

    /// Closes the transaction.
    pub fn close(&mut self) {
        if self.result_ch_tx.is_some() {
            self.result_ch_tx.take();
        }
    }

    /// Returns the number of retransmission it has made.
    pub fn retries(&self) -> u16 {
        self.n_rtx.load(Ordering::SeqCst)
    }
}

/// `TransactionMap` is a thread-safe transaction map.
#[derive(Default, Debug)]
pub struct TransactionMap {
    tr_map: HashMap<String, Transaction>,
}

impl TransactionMap {
    /// Create a new [`TransactionMap`].
    pub fn new() -> TransactionMap {
        TransactionMap {
            tr_map: HashMap::new(),
        }
    }

    /// Inserts a [`Transaction`] to the map.
    pub fn insert(&mut self, key: String, tr: Transaction) -> bool {
        self.tr_map.insert(key, tr);
        true
    }

    /// Looks up a [`Transaction`] by its key.
    pub fn find(&self, key: &str) -> Option<&Transaction> {
        self.tr_map.get(key)
    }

    /// Gets the [`Transaction`] associated with the given `key`.
    pub fn get(&mut self, key: &str) -> Option<&mut Transaction> {
        self.tr_map.get_mut(key)
    }

    /// Deletes a [`Transaction`] by its key.
    pub fn delete(&mut self, key: &str) -> Option<Transaction> {
        self.tr_map.remove(key)
    }

    /// Closes and deletes all [`Transaction`]s.
    pub fn close_and_delete_all(&mut self) {
        for tr in self.tr_map.values_mut() {
            tr.close();
        }
        self.tr_map.clear();
    }

    /// Returns its length.
    pub fn size(&self) -> usize {
        self.tr_map.len()
    }
}
