"""Evaluate the links based on the given environment."""
from __future__ import annotations

import dataclasses as dc
import logging
import os
import sys
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)

from unearth.link import Link
from unearth.pep425tags import get_supported
from unearth.utils import strip_extras

logger = logging.getLogger(__package__)


def is_equality_specifier(specifier: SpecifierSet) -> bool:
    return any(s.operator in ("==", "===") for s in specifier)


def parse_version_from_egg_info(egg_info: str, canonical_name: str) -> str | None:
    for i, c in enumerate(egg_info):
        if canonicalize_name(egg_info[:i]) == canonical_name and c in {"-", "_"}:
            return egg_info[i + 1 :]
    return None


@dc.dataclass
class TargetPython:
    """Target Python to get the candidates.

    Attributes:
        py_ver: Python version tuple, e.g. ``(3, 9)``.
        platforms: List of platforms, e.g. ``['linux_x86_64']``.
        impl: Implementation, e.g. ``'cp'``.
        abis: List of ABIs, e.g. ``['cp39']``.
    """

    py_ver: tuple[int, ...] | None = None
    abis: list[str] | None = None
    impl: str | None = None
    platforms: list[str] | None = None

    def __post_init__(self):
        self._valid_tags: list[Tag] | None = None

    def supported_tags(self) -> list[Tag]:
        if self._valid_tags is None:
            if self.py_ver is None:
                py_version = None
            else:
                py_version = "".join(map(str, self.py_ver[:2]))
            self._valid_tags = get_supported(
                py_version, self.platforms, self.impl, self.abis
            )
        return self._valid_tags


@dc.dataclass(frozen=True)
class Package:
    """A package instance has a name, version, and link that can be downloaded
    or unpacked.

    Attributes:
        name: The name of the package.
        version: The version of the package, or ``None`` if the requirement is a link.
        link: The link to the package.
    """

    name: str
    version: str | None
    link: Link

    def as_json(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the package."""
        return {
            "name": self.name,
            "version": self.version,
            "link": self.link.as_json(),
        }


@dc.dataclass(frozen=True)
class FormatControl:
    only_binary: bool = False
    no_binary: bool = False

    def __post_init__(self):
        if self.only_binary and self.no_binary:
            raise ValueError(
                "Not allowed to set only_binary and no_binary at the same time."
            )

    def is_allowed(self, link: Link, project_name: str) -> bool:
        if self.only_binary:
            if not link.is_wheel:
                logger.debug("Only binaries are allowed for %s", project_name)
            return link.is_wheel
        if self.no_binary:
            if link.is_wheel:
                logger.debug("No binary is allowed for %s", project_name)
            return not link.is_wheel
        return True


@dc.dataclass
class Evaluator:
    """Evaluate the links based on the given environment.

    Args:
        package_name (str): The links must match the package name
        target_python (TargetPython): The links must match the target Python
        hashes (dict[str, list[str]): The links must have the correct hashes
        ignore_compatibility (bool): Whether to ignore the compatibility check
        allow_yanked (bool): Whether to allow yanked candidates
        format_control (bool): Format control flags
    """

    package_name: str
    target_python: TargetPython = dc.field(default_factory=TargetPython)
    hashes: dict[str, list[str]] = dc.field(default_factory=dict)
    ignore_compatibility: bool = False
    allow_yanked: bool = False
    format_control: FormatControl = dc.field(default_factory=FormatControl)

    def __post_init__(self) -> None:
        self._canonical_name = canonicalize_name(self.package_name)

    def _check_yanked(self, link: Link) -> bool:
        if link.yank_reason is not None and not self.allow_yanked:
            logger.debug("%s is yanked", link.redacted)
            return False
        return True

    def _check_requires_python(self, link: Link) -> bool:
        if not self.ignore_compatibility and link.requires_python:
            py_ver = self.target_python.py_ver or sys.version_info[:2]
            py_version = ".".join(str(v) for v in py_ver)
            if not SpecifierSet(link.requires_python).contains(py_version, True):
                logger.debug(
                    "The target python version(%s) doesn't match "
                    "the requires-python specifier %s",
                    py_version,
                    link.requires_python,
                )
                return False
        return True

    def _check_hashes(self, link: Link) -> bool:
        if not self.hashes or not link.hash_name:
            return True
        given_hash = link.hash
        allowed_hashes = self.hashes.get(link.hash_name, [])
        if given_hash not in allowed_hashes:
            logger.debug(
                "Hash mismatch: expected: %s, got: %s:%s",
                allowed_hashes,
                link.hash_name,
                given_hash,
            )
            return False
        return True

    def evaluate_link(self, link: Link) -> Package | None:
        """
        Evaluate the link and return the package if it matches or None if it doesn't.
        """
        if not self.format_control.is_allowed(link, self.package_name):
            return None
        if not self._check_yanked(link):
            return None
        if not self._check_requires_python(link):
            return None
        version: str | None = None
        if link.is_wheel:
            try:
                wheel_info = parse_wheel_filename(link.filename)
            except InvalidWheelFilename as e:
                logger.debug(str(e))
                return None
            if self._canonical_name != wheel_info[0]:
                logger.debug("The package name doesn't match %s", wheel_info[0])
                return None
            if not self.ignore_compatibility and wheel_info[3].isdisjoint(
                self.target_python.supported_tags()
            ):
                logger.debug(
                    "none of the wheel tags(%s) are compatible",
                    ", ".join(sorted(str(tag) for tag in wheel_info[3])),
                )
                return None
            version = str(wheel_info[1])
        else:
            if link._fragment_dict.get("egg"):
                egg_info = strip_extras(link._fragment_dict["egg"])
            else:
                egg_info = os.path.splitext(link.filename)[0]
            version = parse_version_from_egg_info(egg_info, self._canonical_name)
            if version is None:
                logger.debug("Missing version in the filename %s", egg_info)
                return None
        if not self._check_hashes(link):
            return None
        return Package(name=self.package_name, version=version, link=link)


def evaluate_package(
    package: Package, requirement: Requirement, allow_prereleases: bool | None = None
) -> bool:
    """Evaluate the package based on the requirement.

    Args:
        package (Package): The package to evaluate
        requirement (Requirement): The requirement to evaluate against
        allow_prerelease (bool|None): Whether to allow prereleases,
            or None to infer from the specifier.
    Returns:
        bool: True if the package matches the requirement, False otherwise
    """
    if requirement.name:
        if canonicalize_name(package.name) != canonicalize_name(requirement.name):
            return False

    if requirement.specifier and package.version:
        return requirement.specifier.contains(
            package.version, prereleases=allow_prereleases
        )
    return True