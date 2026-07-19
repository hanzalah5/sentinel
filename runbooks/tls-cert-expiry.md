# TLS certificate expiring or expired

## Symptoms
- Alert that a certificate expires within N days, or clients report TLS errors
  ("certificate has expired", "cert is not yet valid").
- Failures may be intermittent if only some endpoints/instances serve the old cert.

## Likely causes
- Automated renewal (e.g., ACME/cert-manager) did not complete.
- Manual certificate that nobody rotated.
- Clock skew making a valid cert appear expired/not-yet-valid.
- New cert issued but not deployed to all instances / load balancers.

## Investigation
- Check the certificate's notAfter date and which instances serve which cert.
- Inspect the renewal system's logs for the last successful/failed renewal.
- Verify system clocks (NTP) on affected hosts.

## Mitigation
- Trigger or complete renewal; deploy the new certificate everywhere it's served.
- Fix the renewal automation so it doesn't recur; alert with more lead time (e.g., 30 days).
