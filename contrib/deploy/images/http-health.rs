use std::env;
use std::io::{BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

fn main() -> std::io::Result<()> {
    let mut args = env::args().skip(1);
    let address: SocketAddr = args.next().expect("address").parse().expect("socket address");
    let path = args.next().expect("path");
    let timeout = Duration::from_secs(2);
    let mut stream = TcpStream::connect_timeout(&address, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    write!(stream, "GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n")?;
    let mut status = String::new();
    BufReader::new(stream).read_line(&mut status)?;
    if status.split_whitespace().nth(1) == Some("200") {
        Ok(())
    } else {
        Err(std::io::Error::other("HTTP check failed"))
    }
}
