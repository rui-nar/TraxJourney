# Zero-knowledge encryption (issue #26)

TraxJourney can encrypt your **memories** and **journal entries** so that even
someone operating the server — an administrator with full database and disk
access — cannot read them. Encryption and decryption happen entirely on your
device; the server only ever stores ciphertext and wrapped keys, and performs no
cryptography itself.

Turn it on under **Settings → Encryption → Set up encryption**.

## What is protected

- **Memory text**: the title (`name`) and notes (`description`).
- **Journal text**: the entry body (`description`).

Once enabled, these are stored as opaque ciphertext. Running raw SQL against the
database shows only encrypted blobs for these fields.

## What is NOT protected (by design)

Zero-knowledge encryption hides **content**, not the **shape** of the data:

- **Metadata** the server structurally needs: which rows exist, their sizes,
  timestamps, dates, ownership, and relationships (a memory belongs to a
  project, etc.).
- **Trip/project names** — out of scope for v1 (encrypting them would require
  re-keying the entire addressing scheme). Choose trip titles accordingly.
- **GPS tracks, photos, elevation** — not encrypted in v1.
- **Public / shared content** — anything you deliberately share is readable by
  whoever you share it with, by definition.
- **A compromised device** — if your unlocked device or its OS keystore is
  compromised, the attacker can read what you can.

## How it works

- A random **Content Master Key (CMK)** encrypts your data (via per-item keys,
  XChaCha20-Poly1305). The server never sees the CMK.
- The CMK is **wrapped** (encrypted) to:
  - **each trusted device** — an X25519 key kept in the OS keystore, so daily
    use is automatic and passwordless; and
  - **a recovery method** you choose (below).
- **New device**: sign in, and it registers itself; approve it from an already
  trusted device (which re-wraps the CMK to it). No password typing.
- **Lost all devices**: use your recovery method to unlock and re-trust a device.

## Recovery — you pick the security level

The recovery method *is* the security level, because how you can get back in
determines who else can. Set at enable time:

| Level | Method | Zero-knowledge? | Notes |
|------|--------|-----------------|-------|
| **High** | Recovery key **or** passphrase | ✅ Yes, strongest | A key/passphrase only you hold. Lose it *and* all devices ⇒ data is unrecoverable **by design**. |
| **Medium** | Security questions | ⚠️ Yes, but weaker | Answers are low-entropy; someone with the server database can attempt an offline brute-force (slowed by Argon2id, not made strong). |
| **Low** | Email reset | ❌ **No** | *Not yet available.* Would let the server recover your key — meaning the admin **could** read your data. Encryption *at rest*, not *from the operator*. |

The recovery key is shown **once** at setup — save it (password manager / print).
The app keeps only the *encrypted* copy, never the plaintext key.

## Status / limitations (pre-release)

- **v1 scope**: memory + journal text only.
- **Low tier** (email recovery) is presented but disabled — it needs a server
  key-escrow + email subsystem (tracked separately).
- The recovery-key is currently rendered as grouped hex; a BIP39 word phrase is
  the intended final format.
- **Not yet validated on real Android/iOS hardware**: Argon2id timing, native↔web
  ciphertext interop with the production KDF, web secure-storage clear-data
  behaviour, and `flutter build web --wasm` bundle size. These must be confirmed
  before shipping to users.
