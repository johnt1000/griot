# Credentials

How griot stores the keys it calls APIs with, and how it keeps credentials out
of what it indexes. Which profile needs which credential:
[Configuration](configuration.md); the tokens `griot index platform` uses:
[Platforms](platforms.md#tokens).

## `griot auth`

```bash
griot auth set openai      # hidden input; OS keychain when available, else <config>/.env at 0600
griot auth list            # status per provider, keys always masked
griot auth remove openai   # asks first; --yes skips the question
griot auth migrate         # moves every credential already in the plaintext file into the keychain
```

Credentials go to the OS keychain — macOS Keychain, Linux Secret Service,
Windows Credential Manager — through `keyring`, which `pipx install griot-rag`
installs with griot. Where no keychain backend is reachable (Linux without a
running, unlocked Secret Service provider such as GNOME Keyring, KWallet or
KeePassXC; a headless server, a container, CI), griot falls back to
`<config>/.env` in plaintext at mode 0600, and `griot auth set`,
`griot auth list` and `griot doctor` say so, why, and where each credential
is. A credential stored in the file earlier (with no backend reachable, or by
a griot that did not install `keyring`) stays there until you run
`griot auth migrate` (or re-run `griot auth set` for that one provider).

A credential exported in your shell (`export GITHUB_TOKEN=...` in `~/.zshrc`,
say) wins over the one griot stores. When the two differ, `griot auth set`,
`griot auth list`, `griot auth remove`, `griot doctor` and an API that refuses
the key say so, naming the shell file and line, or the direnv `.envrc` (never
the value): remove the export to use the stored key.

## Credentials in indexed content

```bash
griot audit                      # where the index holds credential-looking values (never the values)
```

Text is scanned for credential-shaped values (private keys, a list of provider
token formats, JWTs, passwords in URLs, random-looking values assigned to
names like `API_KEY`) before it is embedded and stored, and they are replaced
with a marker; the run tells you where. `griot audit` looks for the same
shapes in what is already indexed. What this can and cannot find is in
[SECURITY.md](../SECURITY.md#credentials-in-what-is-indexed).
