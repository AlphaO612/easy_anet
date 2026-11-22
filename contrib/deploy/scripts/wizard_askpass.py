#!/usr/bin/python3
"""Answer one OpenSSH password prompt using the wizard's private local socket."""
import getpass
import os
import re
import socket
import sys
import warnings


def respond(prompt):
    kind = os.environ.get("SSH_ASKPASS_PROMPT")
    if kind not in ("confirm", "none") and re.search(r"\bpassword:\s*$", prompt, re.I):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(os.environ["ANET_ASKPASS_SOCKET"])
            with client.makefile("rb") as stream:
                sys.stdout.buffer.write(stream.read())
        return
    # Host-key confirmation, OTP and key passphrases belong to the operator.
    with open("/dev/tty", "w") as out:
        if kind == "none":
            print(prompt, file=out)
        elif kind == "confirm" or "yes/no" in prompt.lower():
            print(prompt, end=" ", file=out, flush=True)
            with open("/dev/tty") as terminal:
                print(terminal.readline().strip())
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                print(getpass.getpass(prompt + " ", stream=out))


if __name__ == "__main__":
    respond(sys.argv[1])
