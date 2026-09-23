# Security and caveats

Notification Hub is a single-user service. Its signing keys authenticate clients
that read or answer notifications; they do not authenticate the tools that send
them. Whether it is suitable for an approval gate depends on where the agent,
trusted client, signing key, server, and protected action run.

## When an approval gate helps

The server stores each client's public key. A client must sign requests to read
notifications (`read` scope) or answer them (`respond` scope). Responses also
require a signed, single-use nonce, which prevents replaying a captured response
request. The private key should stay on the trusted client machine.

A remote agent can send an approval request without being able to approve it
when it cannot read the private key or induce the trusted client to sign for it.
Keep the key out of producer hosts, containers, shared filesystems, and accounts
the agent can access. If the agent and client run under the same OS account,
private-key file permissions alone usually provide no separation. Separate OS
accounts can work if their filesystem and process permissions actually keep the
agent away from the key and client; a separate trusted machine is easier to
reason about.

The gate itself must also be protected. If an agent can edit or skip the wrapper
that waits for approval, or run the protected action directly, a signed response
cannot enforce the gate. For example, an approval wrapper only protects a
deployment if the agent cannot invoke the deployment command by another route.

## Producer requests and displayed content

Creating notifications does not require a key. Anyone who can reach the server
can submit an approval request and choose its displayed domain, sender, message,
and response options. Those fields are labels supplied by the producer, not
proof of its identity. Treat message text and links as untrusted, and verify
consequential requests through a separate trusted channel before approving.

The producer API also allows anyone with a notification ID to poll its outcome,
including any response message, or cancel it while pending. IDs are random but
are returned to their creators, so they are not authorization credentials. Do
not put secrets in response messages. Creation and pending-request limits bound
some abuse but do not protect an Internet-exposed server from untrusted traffic.

## Deployment and key protection

Keep the server on loopback or a trusted network. For remote connections, use
an encrypted tunnel, such as SSH or a trusted VPN, between clients and the
server. Signatures authenticate client requests; they do not encrypt
notification content or outcomes in transit. A conventional TLS-terminating
reverse proxy currently breaks signed client requests: clients sign an `https`
URL, but the built-in server verifies the internal `http` URL and does not honor
`X-Forwarded-Proto`. If you use TLS for a supported connection, leave
certificate verification enabled on clients and notifiers.

Protect the server database and configuration from untrusted local users. The
default strict database permission checks help prevent group or other users
from reading the database. Give each signing key only the scopes it needs, and
keep unencrypted client private keys readable only by their trusted owner. See
[Configuration](configuration.md) for network and permission settings and
[Creating client signing keys](signing-keys.md) for key setup and rotation.
