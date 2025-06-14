# SPDX-License-Identifier: LGPL-2.1-or-later

import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from mkosi.config import Architecture, Config
from mkosi.context import Context
from mkosi.curl import curl
from mkosi.distributions import (
    DistributionInstaller,
    PackageType,
    join_mirror,
)
from mkosi.installer.dnf import Dnf
from mkosi.installer.rpm import RpmRepository, find_rpm_gpgkey, setup_rpm
from mkosi.log import die
from mkosi.util import startswith, tuplify


@tuplify
def find_fedora_rpm_gpgkeys(context: Context) -> Iterable[str]:
    key1 = find_rpm_gpgkey(
        context, key=f"RPM-GPG-KEY-fedora-{context.config.release}-primary", required=False
    )
    key2 = find_rpm_gpgkey(
        context, key=f"RPM-GPG-KEY-fedora-{context.config.release}-secondary", required=False
    )

    versionre = re.compile(r"RPM-GPG-KEY-fedora-(\d+)-(primary|secondary)")

    if key1:
        # During branching, there is always a kerfuffle with the key transition.
        # For Rawhide, try to load the N+1 key, just in case our local configuration
        # still indicates that Rawhide==N, but really Rawhide==N+1.
        if context.config.release == "rawhide" and (rhs := startswith(key1, "file://")):
            if m := versionre.match(Path(rhs).name):
                version = int(m.group(1))
                if key3 := find_rpm_gpgkey(
                    context,
                    key=f"RPM-GPG-KEY-fedora-{version + 1}-primary",
                    required=False,
                ):
                    yield key3

        yield key1

    if key2:
        yield key2

    if not key1 and not key2:
        if not context.config.repository_key_fetch:
            die(
                "Fedora GPG keys not found in /usr/share/distribution-gpg-keys",
                hint="Make sure the distribution-gpg-keys package is installed",
            )

        if context.config.release == "rawhide":
            # https://fedoraproject.org/fedora.gpg is always outdated when the rawhide key changes. Instead,
            # let's fetch it from distribution-gpg-keys on github, which is generally up-to-date.
            keys = "https://raw.githubusercontent.com/rpm-software-management/distribution-gpg-keys/main/keys/fedora"

            # The rawhide key is a symlink and github doesn't redirect those to the actual file for some
            # reason, so we fetch the file and read the release it points to ourselves.
            with tempfile.TemporaryDirectory() as d:
                curl(context.config, f"{keys}/RPM-GPG-KEY-fedora-rawhide-primary", Path(d))
                key = (Path(d) / "RPM-GPG-KEY-fedora-rawhide-primary").read_text()

            yield f"{keys}/{key}"

            # Same as above, the symlink in distribution-gpg-keys might not have been updated yet to point to
            # the new rawhide key when branching happens, so try to load the N+1 key as well.
            if m := versionre.match(key):
                version = int(m.group(1))

                try:
                    with tempfile.TemporaryDirectory() as d:
                        curl(
                            context.config,
                            f"{keys}/RPM-GPG-KEY-fedora-{version + 1}-primary",
                            Path(d),
                            log=False,
                        )

                    yield f"{keys}/RPM-GPG-KEY-fedora-{version + 1}-primary"
                except subprocess.CalledProcessError:
                    pass
        else:
            yield "https://fedoraproject.org/fedora.gpg"


class Installer(DistributionInstaller):
    @classmethod
    def pretty_name(cls) -> str:
        return "Fedora Linux"

    @classmethod
    def filesystem(cls) -> str:
        return "ext4"

    @classmethod
    def package_type(cls) -> PackageType:
        return PackageType.rpm

    @classmethod
    def default_release(cls) -> str:
        return "rawhide"

    @classmethod
    def grub_prefix(cls) -> str:
        return "grub2"

    @classmethod
    def package_manager(cls, config: Config) -> type[Dnf]:
        return Dnf

    @classmethod
    def setup(cls, context: Context) -> None:
        setup_rpm(context)
        Dnf.setup(
            context,
            list(cls.repositories(context)),
            filelists=False,
            metadata_expire="6h" if context.config.release == "rawhide" else None,
        )

    @classmethod
    def install(cls, context: Context) -> None:
        Dnf.install(context, ["basesystem"], apivfs=False)

    @classmethod
    def repositories(cls, context: Context) -> Iterable[RpmRepository]:
        gpgurls = find_fedora_rpm_gpgkeys(context)

        if context.config.local_mirror:
            yield RpmRepository("fedora", f"baseurl={context.config.local_mirror}", gpgurls)
            return

        if context.config.release == "eln":
            mirror = (
                context.config.mirror
                or "https://odcs.fedoraproject.org/composes/production/latest-Fedora-ELN/compose"
            )
            for repo in ("Appstream", "BaseOS", "Extras", "CRB"):
                url = f"baseurl={join_mirror(mirror, repo)}"
                yield RpmRepository(repo.lower(), f"{url}/$basearch/os", gpgurls)
                yield RpmRepository(repo.lower(), f"{url}/$basearch/debug/tree", gpgurls, enabled=False)
                yield RpmRepository(repo.lower(), f"{url}/source/tree", gpgurls, enabled=False)
        elif m := context.config.mirror:
            directory = "development" if context.config.release == "rawhide" else "releases"
            url = f"baseurl={join_mirror(m, f'linux/{directory}/$releasever/Everything')}"
            yield RpmRepository("fedora", f"{url}/$basearch/os", gpgurls)
            yield RpmRepository("fedora-debuginfo", f"{url}/$basearch/debug/tree", gpgurls, enabled=False)
            yield RpmRepository("fedora-source", f"{url}/source/tree", gpgurls, enabled=False)

            if context.config.release != "rawhide":
                url = f"baseurl={join_mirror(m, 'linux/updates/$releasever/Everything')}"
                yield RpmRepository("updates", f"{url}/$basearch", gpgurls)
                yield RpmRepository("updates-debuginfo", f"{url}/$basearch/debug", gpgurls, enabled=False)
                yield RpmRepository("updates-source", f"{url}/source/tree", gpgurls, enabled=False)

                url = f"baseurl={join_mirror(m, 'linux/updates/testing/$releasever/Everything')}"
                yield RpmRepository("updates-testing", f"{url}/$basearch", gpgurls, enabled=False)
                yield RpmRepository(
                    "updates-testing-debuginfo", f"{url}/$basearch/debug", gpgurls, enabled=False
                )
                yield RpmRepository("updates-testing-source", f"{url}/source/tree", gpgurls, enabled=False)
        else:
            url = "metalink=https://mirrors.fedoraproject.org/metalink?arch=$basearch"
            yield RpmRepository("fedora", f"{url}&repo=fedora-$releasever", gpgurls)
            yield RpmRepository(
                "fedora-debuginfo", f"{url}&repo=fedora-debug-$releasever", gpgurls, enabled=False
            )
            yield RpmRepository(
                "fedora-source", f"{url}&repo=fedora-source-$releasever", gpgurls, enabled=False
            )

            if context.config.release != "rawhide":
                yield RpmRepository("updates", f"{url}&repo=updates-released-f$releasever", gpgurls)
                yield RpmRepository(
                    "updates-debuginfo",
                    f"{url}&repo=updates-released-debug-f$releasever",
                    gpgurls,
                    enabled=False,
                )
                yield RpmRepository(
                    "updates-source",
                    f"{url}&repo=updates-released-source-f$releasever",
                    gpgurls,
                    enabled=False,
                )
                yield RpmRepository(
                    "updates-testing", f"{url}&repo=updates-testing-f$releasever", gpgurls, enabled=False
                )
                yield RpmRepository(
                    "updates-testing-debuginfo",
                    f"{url}&repo=updates-testing-debug-f$releasever",
                    gpgurls,
                    enabled=False,
                )
                yield RpmRepository(
                    "updates-testing-source",
                    f"{url}&repo=updates-testing-source-f$releasever",
                    gpgurls,
                    enabled=False,
                )

    @classmethod
    def architecture(cls, arch: Architecture) -> str:
        a = {
            Architecture.arm64:     "aarch64",
            Architecture.mips64_le: "mips64el",
            Architecture.mips_le:   "mipsel",
            Architecture.ppc64_le:  "ppc64le",
            Architecture.riscv64:   "riscv64",
            Architecture.s390x:     "s390x",
            Architecture.x86_64:    "x86_64",
        }.get(arch)  # fmt: skip

        if not a:
            die(f"Architecture {a} is not supported by Fedora")

        return a
