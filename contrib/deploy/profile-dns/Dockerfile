FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS packages
RUN apk add --no-cache --initdb --root /runtime --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories py3-dnspython
FROM scratch
COPY --from=packages /runtime/ /
COPY --chmod=0444 server.py /app/server.py
USER 65532:65532
ENTRYPOINT ["/usr/bin/python3","-B","/app/server.py"]
