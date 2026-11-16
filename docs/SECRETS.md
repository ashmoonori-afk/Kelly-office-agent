# Local secret storage

Windows keeps v0.1 `auth/<provider>.dpapi` bytes: canonical JSON, current-user
DPAPI, no optional entropy, and the UI-forbidden flag.

macOS stores that payload as a Security.framework generic-password item, using
`ctypes`, `SecKeychainFindGenericPassword`, and `SecKeychainItemDelete`.
Creation uses exported legacy `SecKeychainItemCreateNew`,
`SecKeychainItemSetAttribute`, and `SecKeychainItemAddNoUI` functions.
These are private compatibility exports: future macOS support needs verification.
The public AddGenericPassword, SecKeychainItemCreateFromContent and SecItemAdd
file-keychain paths can silently fall back to the default keychain if the selected
keychain disappears. AddNoUI targets only its explicit handle, including that
failure window.
The default is `~/Library/Keychains/login.keychain-db`.
Set `KELLY_KEYCHAIN_PATH` to an absolute path to an existing, unlocked keychain
to use a dedicated production keychain. Keep this setting consistent across
server and CLI processes. Kelly never creates or unlocks it, prompts for access,
changes the default keychain, or falls back if the selected keychain fails.

On macOS the `.dpapi` filename is retained, but contains only a 42-byte versioned
random reference, never a password or a keychain path. The item's service is
bound to the absolute credential-slot path. Lookups and deletion search only the
selected keychain and service. Copying a reference to another home or provider,
or selecting another keychain, does not grant access to the original item.
Moving a home requires saving its credentials again.

Config serializes macOS readers and writers with an advisory directory lock.
It publishes the new reference before retiring the old item; failed publication
deletes the new item. Clear deletes the item before its reference and is repeatable.
The Keychain and filesystem are not one transaction: process termination or a
Keychain deletion error between publication and retirement can leave an orphan
item. Deletion errors are reported. Use clear rather than deleting references manually.

Tests create/unlock/delete temporary keychains; autouse selection reaches child processes.
They cover native roundtrip, retirement, isolation, failed publication, disappearance,
and plaintext absence without sleeps.
