# Development Status

## OPS-UDA-001 — UDA subpath compatibility
Status: IN PROGRESS

Flask accepts a trusted single UDA/Caddy proxy's forwarded prefix and generates prefix-aware assets and navigation. Client-side API requests and downloads resolve relative to the application root. Direct LAN root mode remains supported.

Evidence: branch changes, UDA/LAN smoke tests, GitHub Actions workflow.
Security: forwarded headers only trusted if backend ingress is proxy-restricted; public route configuration unchanged.

- [ ] Tests pass in CI and code merges to main for Ubuntu UDA.
- [ ] User validates upload, progress, download and settings through UDA.
