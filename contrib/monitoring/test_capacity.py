"""Verify the shipped mTLS endpoint, fixed transfer and recording rule on an internal Docker network."""
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
NGINX = "nginxinc/nginx-unprivileged:1.28-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8"
BLACKBOX = "prom/blackbox-exporter:v0.27.0@sha256:a50c4c0eda297baa1678cd4dc4712a67fdea713b832d43ce7fcc5f9bea05094d"
PROM = "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.STDOUT)


def tls(directory):
    def run(*args):
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)
    run("req","-x509","-nodes","-newkey","rsa:2048","-days","1","-subj","/CN=isolated test CA",
        "-keyout","ca.key","-out","ca.crt")
    for role in ("server","client"):
        run("req","-nodes","-newkey","rsa:2048","-subj","/CN="+role,"-keyout",role+".key","-out",role+".csr")
        (directory/"extensions").write_text("subjectAltName=DNS:target\nextendedKeyUsage="+role+"Auth\n")
        run("x509","-req","-in",role+".csr","-CA","ca.crt","-CAkey","ca.key","-CAcreateserial",
            "-days","1","-extfile","extensions","-out",role+".crt")
        # The temporary keys must be accessible to the container UID through individual binds.
        (directory/(role+".key")).chmod(0o444)


def main():
    network = "anet-cap-test-" + secrets.token_hex(5)
    containers = []
    docker("network","create","--internal",network)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tls(directory)
            payload = directory/"payload"
            payload.write_bytes(os.urandom(65536))
            def start(alias, image, mounts, command):
                args = ["run","--pull","never","-d","--network",network,"--network-alias",alias]
                for source, target in mounts:
                    args.extend(["-v",f"{source}:{target}:ro"])
                containers.append(docker(*args,image,*command).strip())
            def certs(role, remote):
                return [(directory/file, remote+"/"+file) for file in ("ca.crt",role+".crt",role+".key")]
            start("target",NGINX,[(ROOT/"capacity.conf","/etc/nginx/capacity.conf"),(payload,"/etc/anet/payload.bin"),
                                  *certs("server","/etc/anet/tls")],["nginx","-c","/etc/nginx/capacity.conf","-g","daemon off;"])
            start("probe",BLACKBOX,[(ROOT/"blackbox.yml","/etc/blackbox.yml"),*certs("client","/etc/anet/capacity-tls")],
                  ["--config.file=/etc/blackbox.yml"])
            def measure(path):
                url="http://probe:9115/probe?module=https_capacity&target=https%3A%2F%2Ftarget%3A9444"+path
                return docker("exec",containers[0],"wget","-qO-","-T","4",url)
            result=""
            for _ in range(15):
                try:
                    result=measure("/anet/capacity.bin")
                    if re.search(r"^probe_success 1$",result,re.M): break
                except subprocess.CalledProcessError:
                    pass
                time.sleep(1)
            assert re.search(r"^probe_success 1$",result,re.M), result
            assert re.search(r"^probe_http_uncompressed_body_length 65536$",result,re.M), result
            assert re.search(r"^probe_success 0$",measure("/other"),re.M)
            # Verify rejection at the destination when no client credential is presented.
            try:
                docker("exec",containers[0],"wget","--no-check-certificate","-qO-","https://target:9444/anet/capacity.bin")
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("endpoint accepted anonymous request")
            docker("run","--pull","never","--rm","--network","none","-v",f"{ROOT}:/rules:ro","-w","/rules",
                   "--entrypoint","/bin/promtool",PROM,"test","rules","rules.test.yml")
            print("Verified authenticated 65536-byte HTTPS transfer, rejected path and anonymous request, and recording rules.")
    finally:
        for cid in reversed(containers):
            docker("rm","-f",cid)
        docker("network","rm",network)


if __name__ == "__main__":
    main()
