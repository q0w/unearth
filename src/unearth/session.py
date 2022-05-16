from __future__ import annotations

import email
import io
import ipaddress
import logging
import mimetypes
import os
from datetime import timedelta
from typing import Any, Iterable

import urllib3
from requests import Session
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.models import PreparedRequest, Response
from requests_cache import CacheMixin

from unearth.auth import MultiDomainBasicAuth
from unearth.link import Link
from unearth.utils import build_url_from_netloc, parse_netloc

logger = logging.getLogger(__package__)

DEFAULT_MAX_RETRIES = 5
DEFAULT_CACHE_EXPIRE = timedelta(days=7)

DEFAULT_SECURE_ORIGINS = [
    ("https", "*", "*"),
    ("wss", "*", "*"),
    ("*", "localhost", "*"),
    ("*", "127.0.0.0/8", "*"),
    ("*", "::1/128", "*"),
    ("file", "*", "*"),
]


def _compare_origin_part(allowed: str, actual: str) -> bool:
    return allowed == "*" or allowed == actual


class InsecureMixin:
    def cert_verify(self, conn, url, verify, cert):
        return super().cert_verify(conn, url, verify=False, cert=cert)


class InsecureHTTPAdapter(InsecureMixin, HTTPAdapter):
    pass


class LocalFSAdapter(BaseAdapter):
    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        link = Link(request.url)
        path = link.file_path
        resp = Response()
        resp.status_code = 200
        resp.url = request.url
        resp.request = request

        try:
            stats = os.stat(path)
        except OSError as exc:
            # format the exception raised as a io.BytesIO object,
            # to return a better error message:
            resp.status_code = 404
            resp.reason = type(exc).__name__
            resp.raw = io.BytesIO(f"{resp.reason}: {exc}".encode("utf8"))
        else:
            modified = email.utils.formatdate(stats.st_mtime, usegmt=True)
            content_type = mimetypes.guess_type(path)[0] or "text/plain"
            resp.headers.update(
                {
                    "Content-Type": content_type,
                    "Content-Length": stats.st_size,
                    "Last-Modified": modified,
                }
            )

            resp.raw = open(path, "rb")
            resp.close = resp.raw.close

        return resp

    def close(self) -> None:
        pass


class PyPISession(CacheMixin, Session):
    """
    A session with caching enabled and specific hosts trusted.

    Args:
        index_urls: The PyPI index URLs to use.
        retries: The number of retries to attempt.
        use_cache_dir: Whether to use the cache directory.
        expire_after: The amount of time to cache responses.
        cache_control: Whether to use the cache-control header.
        trusted_hosts: The hosts to trust.
    """

    def __init__(
        self,
        *,
        index_urls: Iterable[str] = (),
        retries: int = DEFAULT_MAX_RETRIES,
        use_cache_dir: bool = True,
        expire_after: timedelta = DEFAULT_CACHE_EXPIRE,
        cache_control: bool = True,
        trusted_hosts: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(
            use_cache_dir=use_cache_dir,
            expire_after=expire_after,
            cache_control=cache_control,
            **kwargs,
        )

        retry = urllib3.Retry(
            total=retries,
            # A 500 may indicate transient error in Amazon S3
            # A 520 or 527 - may indicate transient error in CloudFlare
            status_forcelist=[500, 503, 520, 527],
            backoff_factor=0.25,
        )
        self._insecure_adapter = InsecureHTTPAdapter(max_retries=retry)
        self._trusted_host_ports: set[tuple[str, int | None]] = set()

        self.mount("file://", LocalFSAdapter())
        for host in trusted_hosts:
            self.add_trusted_host(host)
        self.auth = MultiDomainBasicAuth(index_urls=index_urls)

    def add_trusted_host(self, host: str) -> None:
        """Trust the given host by not verifying the SSL certificate."""
        hostname, port = parse_netloc(host)
        self._trusted_host_ports.add((hostname, port))
        for scheme in ("https", "http"):
            url = build_url_from_netloc(host, scheme=scheme)
            self.mount(url + "/", self._insecure_adapter)
            if port is None:
                # Allow all ports for this host
                self.mount(url + ":", self._insecure_adapter)

    def iter_secure_origins(self) -> Iterable[tuple[str, str, str]]:
        yield from DEFAULT_SECURE_ORIGINS
        for host, port in self._trusted_host_ports:
            yield ("*", host, port or "*")

    def is_secure_origin(self, location: Link) -> bool:
        """
        Determine if the origin is a trusted host.

        Args:
            location (Link): The location to check.
        """
        _, _, scheme = location.parsed.scheme.rpartition("+")
        host, port = location.parsed.hostname, location.parsed.port
        for secure_scheme, secure_host, secure_port in self.iter_secure_origins():
            if not _compare_origin_part(secure_scheme, scheme):
                continue
            try:
                addr = ipaddress.ip_address(host)
                network = ipaddress.ip_network(secure_host)
            except ValueError:
                # Either addr or network is invalid
                if not _compare_origin_part(secure_host, host):
                    continue
            else:
                if addr not in network:
                    continue

            if not _compare_origin_part(secure_port, port or "*"):
                continue
            # We've got here, so all the parts match
            return True

        logger.warning(
            "Skip %s for not being trusted, please add it to `trusted_hosts` list",
            location.redacted,
        )
        return False