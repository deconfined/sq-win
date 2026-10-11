Sequoia utilities for native Windows x64

Extract the ZIP and add its bin directory to PATH. Keep the accompanying
Microsoft runtime DLLs beside the executables. No Rust installation is
required to run the utilities. OpenPGP cryptography uses Windows CNG;
OpenSSL used for DNSSEC is linked statically.

The complete ZIP contains sq.exe, gpg-sq.exe, gpgv-sq.exe, sqv.exe,
sqop.exe, sqopv.exe, sq-wot.exe, sq-cryptoki.exe and sequoia-keystore.exe.

gpg-sq uses GnuPG's gpg-agent and gpgconf for secret-key operations.
Install native Windows GnuPG separately and add its bin directory to PATH.
For interactive passphrase prompts, its pinentry must also be installed.
These GnuPG components are not included in this Sequoia download.
GNUPGHOME overrides the default %APPDATA%\gnupg directory.

Chameleon includes explicit Windows compatibility patches for native
paths and file descriptors. Parcimonie can run in the foreground with
--x-sequoia-parcimonie; Unix daemonization and automatic daemon startup
are unavailable on Windows. The package omits the development recorder.

sq works with its embedded keystore. The separate sequoia-keystore.exe
server is supplied as an optional utility; use --help for its options.

sq-cryptoki manages PKCS#11 tokens. Install the token vendor's matching
Windows PKCS#11 DLL separately and select it with --module. Vendor drivers
and hardware are not included. CI probes version reporting and token
enumeration with no modules; hardware interoperability is untested.
The bundled sq and keystore server use software-key and GnuPG-agent
backends. Their optional PKCS#11, OpenPGP-card and TPM backends are disabled.

sources.json records exact upstream tags, commits and build configuration.
share/<project> includes source archives, applied patches, licenses,
dependency metadata, and upstream/build Cargo lockfiles.
The binaries are not Authenticode-signed.
