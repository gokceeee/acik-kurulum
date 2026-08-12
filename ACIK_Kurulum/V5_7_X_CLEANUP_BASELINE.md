# V5.7 X Cleanup Baseline - LOCKED

V5.7 is the accepted reference for deleting the legacy X account on this
hardware. Its source is preserved without modification at:

`work\ACIK_KURULUM_V5_7\src\acik_onboarding\services.py`

Reference SHA-256: `D59AE4EB30FF121E1FD4F98FF3ABF6D41DCF2317E08D6078FC333A613F98AA41`

The V5.14 X-cleanup handoff keeps this V5.7 order:

1. Clear Winlogon and LSA AutoLogon values without reading a password.
2. Register and start the SYSTEM X cleanup.
3. Log off X; remove and verify the account and its profile.
4. Apply and verify the documented local-user picker policy.
5. Restart only after all prior conditions succeed.

Do not change this deletion sequence without a verified real-device test and
an explicit replacement baseline.
