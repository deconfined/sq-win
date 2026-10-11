#!/usr/bin/env python3
"""Exercise signing, encryption and rejection of modified data on Windows."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def smoke(binaries, gnupg):
    if os.name != "nt":
        raise RuntimeError("The release smoke tests require native Windows")
    with tempfile.TemporaryDirectory(prefix="sequoia-smoke-") as temporary:
        work = Path(temporary)
        home = work / "gnupg"
        home.mkdir()
        env = os.environ.copy()
        # Exclude development directories from DLL lookup. Only the shipped
        # binaries, the separately installed GnuPG runtime and Windows remain.
        env["PATH"] = os.pathsep.join([str(binaries), str(gnupg),
                                      str(Path(env["SYSTEMROOT"]) / "System32")])
        env.update(GNUPGHOME=str(home), SEQUOIA_HOME=str(work / "sq"),
                   SEQUOIA_CERT_STORE=str(work / "certstore"),
                   SEQUOIA_KEY_STORE=str(work / "keystore"), SEQUOIA_CRYPTO_POLICY="")

        def execute(name, arguments, data=None, reject=False):
            print("+ " + name + " " + " ".join(map(str, arguments)), flush=True)
            result = subprocess.run([str(binaries / (name + ".exe")), *map(str, arguments)],
                                    input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=work, env=env, timeout=120)
            if reject:
                if result.returncode == 0:
                    raise RuntimeError(name + " accepted modified signed data")
                # A crash or missing DLL is not evidence that a verifier rejected a signature.
                if result.returncode < 0 or result.returncode > 255:
                    raise RuntimeError(f"{name} crashed instead of rejecting data: {result.returncode}")
            elif result.returncode:
                raise RuntimeError(f"{name} failed ({result.returncode}): " +
                                   result.stderr.decode("utf-8", errors="replace"))
            return result.stdout

        key = work / "key.asc"
        certificate = work / "cert.asc"
        signature = work / "signature.asc"
        message = work / "message.txt"
        data = b"Sequoia Windows signature and encryption smoke test.\r\n"
        message.write_bytes(data)
        key.write_bytes(execute("sqop", ["generate-key", "Windows CI <ci@example.invalid>"]))
        certificate.write_bytes(execute("sqop", ["extract-cert"], key.read_bytes()))
        signature.write_bytes(execute("sqop", ["sign", "--as=binary", key], data))
        encrypted = execute("sqop", ["encrypt", certificate], data)
        if execute("sqop", ["decrypt", key], encrypted) != data:
            raise RuntimeError("SOP encryption/decryption did not preserve the message")

        verifiers = [
            ("sqop", ["verify", signature, certificate], True),
            ("sqopv", ["verify", signature, certificate], True),
            ("sqv", ["--keyring", certificate, "--signature-file", signature, message], False),
            ("gpgv-sq", ["--status-fd", "1", "--keyring", certificate, signature, message], False),
            ("sq", ["verify", "--signer-file", certificate, "--signature-file", signature, message], False),
        ]
        for name, arguments, stdin in verifiers:
            execute(name, arguments, data if stdin else None)
        modified = data + b"modified\r\n"
        message.write_bytes(modified)
        for name, arguments, stdin in verifiers:
            execute(name, arguments, modified if stdin else None, reject=True)
        message.write_bytes(data)

        listing = execute("gpg-sq", ["--batch", "--with-colons", "--show-keys", certificate])
        fingerprints = [line.split(":")[9] for line in listing.decode().splitlines()
                        if line.startswith("fpr:")]
        if not fingerprints:
            raise RuntimeError("Chameleon did not emit a certificate fingerprint")
        fingerprint = fingerprints[0]
        execute("sq-wot", ["--keyring", certificate, "--trust-root", fingerprint,
                           "authenticate", fingerprint, "--email", "ci@example.invalid"])
        execute("sq-wot", ["--format", "dot", "--keyring", certificate, "--trust-root", fingerprint,
                           "authenticate", fingerprint, "--email", "ci@example.invalid"])

        try:
            execute("gpg-sq", ["--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                               "--import", key])
            chameleon_signature = work / "chameleon.sig"
            execute("gpg-sq", ["--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
                               "--local-user", fingerprint, "--output", chameleon_signature,
                               "--detach-sign", message], b"\n")
            execute("sqopv", ["verify", chameleon_signature, certificate], data)
        finally:
            subprocess.run([str(gnupg / "gpgconf.exe"), "--homedir", str(home), "--kill", "all"],
                           cwd=work, env=env, timeout=30, check=False)
        print("All Windows functional smoke tests passed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--gnupg", type=Path, default=os.environ.get("SEQUOIA_GNUPG_BIN"))
    args = parser.parse_args()
    if not args.gnupg:
        parser.error("--gnupg or SEQUOIA_GNUPG_BIN is required")
    smoke(args.bin.resolve(), args.gnupg.resolve())


if __name__ == "__main__":
    main()
