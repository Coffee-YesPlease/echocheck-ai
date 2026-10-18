"""Container entry point with a validated port and no shell command."""
import os
import ipaddress
import uvicorn

def trusted_proxy_ips(value):
    """Trust only explicit IP addresses/networks; never every connecting client."""
    if not value.strip():
        return ''
    entries = [entry.strip() for entry in value.split(',')]
    try:
        for entry in entries:
            network = ipaddress.ip_network(entry, strict=False)
            if network.prefixlen == 0:
                raise ValueError
    except ValueError:
        raise ValueError('TRUSTED_PROXY_IPS must list explicit proxy IPs or restricted IP networks; wildcard trust is disabled') from None
    return ','.join(entries)

def main():
    try:
        port = int(os.environ.get('PORT', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit('PORT must be an integer from 1 to 65535') from None
    try:
        proxies = trusted_proxy_ips(os.environ.get('TRUSTED_PROXY_IPS', ''))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    uvicorn.run('backend.main:app', host='0.0.0.0', port=port,
                proxy_headers=bool(proxies), forwarded_allow_ips=proxies)

if __name__ == '__main__':
    main()
