# ACIK source delivery (public)

This folder is a public source-code copy of ACIK Kurulum v5.21.31.  It is not
an operational installation package.

## Intentionally excluded

- `app_config.local.json` and `private_secrets`
- Domain, Wi-Fi, local-admin, backup, product-key, webhook, API-token, and
  VPN credentials
- Encrypted VPN profiles, plain VPN exports, certificates, and private keys
- `FORTICLIENT_VPN_PROFILE.md` (internal FortiClient connection name and
  gateway address)
- Third-party installer executables, boot assets, release output, logs, and
  diagnostic records
- The private domain-join helper and automated test fixtures

`app_config.example.json` is the password-free example configuration.  For a
private deployment, keep the real configuration in a protected location and
select it with `ACIK_CONFIG_PATH`; do not put it in this folder.

FortiClient support is source-only here.  A private build must supply its
approved profile separately and provide its export password at run time through
the `ACIK_FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD` environment variable.  The
value is intentionally absent from the code and this delivery.

Before publishing, keep the final file-manifest produced with this folder and
run the same secret scan used for the delivery verification.
