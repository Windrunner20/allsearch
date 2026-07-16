## security Domain Capabilities (4 available)

### security.scan
Submit file hash, URL, IP or domain to 70+ security vendor aggregate scan

**Parameters:**
- `ioc` (required): Indicator of compromise to scan: domain, IP, URL, or file hash

### security.noise
Check if IPv4 address is internet background scanning noise

**Parameters:**
- `ip` (required): Single IPv4 address to check

### security.intel
Threat intelligence for IP, domain, URL or file hash

**Parameters:**
- `ioc` (required): Indicator of compromise

### security.vuln
CVE vulnerability details with CVSS scores, affected versions, patch links, and exploitation status

**Parameters:**
- `type` (required): Query intent: "cve" / "commit" / "package"
- `value` (required): CVE ID / commit hash / ecosystem:name@version
