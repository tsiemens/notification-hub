# Creating client signing keys

Notification Hub clients authenticate signed API requests with a private key.
The server stores only the corresponding public key.

Create a dedicated key for Notification Hub. **Do not reuse an SSH key or any
other existing identity key.** A separate key keeps access, rotation, backup,
and revocation boundaries independent. Common SSH private keys also use the
OpenSSH private-key format, whereas Notification Hub expects an unencrypted PEM
private key.

The examples below create files named `notification-hub-client.key` and
`notification-hub-client.pub`. Run them on the trusted machine where the client
will be used.

## Recommended: Ed25519

Ed25519 keys are small, fast, and the recommended default:

```sh
openssl genpkey -algorithm ED25519 -out notification-hub-client.key
openssl pkey -in notification-hub-client.key -pubout \
  -out notification-hub-client.pub
```

## Other supported algorithms

Notification Hub also supports RSA-PSS with SHA-512, P-256, and P-384. The
client selects the correct signature algorithm from the key type.

RSA (3072 bits):

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out notification-hub-client.key
openssl pkey -in notification-hub-client.key -pubout \
  -out notification-hub-client.pub
```

P-256:

```sh
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 \
  -out notification-hub-client.key
openssl pkey -in notification-hub-client.key -pubout \
  -out notification-hub-client.pub
```

P-384:

```sh
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:secp384r1 \
  -out notification-hub-client.key
openssl pkey -in notification-hub-client.key -pubout \
  -out notification-hub-client.pub
```

## Protect and install the files

Restrict the private key so only its owner can read it:

```sh
chmod 600 notification-hub-client.key
chmod 644 notification-hub-client.pub
```

Keep `notification-hub-client.key` only on the trusted client machine. Do not
copy it into the server or producer environments, commit it to source control,
or include it in container images. Notification Hub loads keys noninteractively,
so encrypted PEM private keys are not supported.

Copy only `notification-hub-client.pub` to the server through a trusted channel.
Configure the server with its path and the minimum scopes the client needs:

```toml
[[auth.signing_keys]]
principal = "desktop-ui"
key_id = "desktop-ui"
public_key_file = "/etc/notification-hub/notification-hub-client.pub"
scopes = ["read", "respond", "read_state"]
```

Configure the client with the same `key_id` and its private-key path:

```toml
[auth]
key_id = "desktop-ui"
private_key_file = "/home/you/.config/notification-hub/notification-hub-client.key"
```

Use different keys when clients should have different scopes or independent
revocation. To rotate a key, generate a new pair, add the new public key to the
server under a new `key_id`, update the client, verify connectivity, and then
remove the old server entry.
