//! Test driver for running the actual controller probe against a disposable
//! private native runtime fixture.

use hermes_core::app::controller::ProbeOutcome;
use hermes_native::probe::NativeProbe;

#[tokio::main]
async fn main() {
    let url = std::env::args().nth(1).expect("fixture URL");
    let base = url::Url::parse(&url).expect("valid fixture URL");
    let ip: std::net::Ipv4Addr = base
        .host_str()
        .expect("fixture host")
        .parse()
        .expect("fixture IPv4");
    assert!(ip.is_private());
    let probe = NativeProbe::fixed(base).expect("probe construction");
    assert!(matches!(
        probe.verify("fixture").await,
        ProbeOutcome::Verified
    ));
}
