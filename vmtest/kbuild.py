# Copyright (c) Facebook, Inc. and its affiliates.
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import filecmp
import logging
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from util import KernelVersion, nproc
from vmtest.config import SUPPORTED_KERNEL_VERSIONS

logger = logging.getLogger(__name__)

STABLE_LINUX_GIT_URL = (
    "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
)


def kconfig() -> str:
    return r"""# Minimal Linux kernel configuration for booting into vmtest and running drgn
# tests.

CONFIG_LOCALVERSION="-vmtest2"

CONFIG_SMP=y
CONFIG_MODULES=y

# We run the tests in KVM.
CONFIG_HYPERVISOR_GUEST=y
CONFIG_KVM_GUEST=y
CONFIG_PARAVIRT=y
CONFIG_PARAVIRT_SPINLOCKS=y

# Minimum requirements for vmtest.
CONFIG_9P_FS=y
CONFIG_DEVTMPFS=y
CONFIG_INET=y
CONFIG_NET=y
CONFIG_NETWORK_FILESYSTEMS=y
CONFIG_NET_9P=y
CONFIG_NET_9P_VIRTIO=y
CONFIG_OVERLAY_FS=y
CONFIG_PCI=y
CONFIG_PROC_FS=y
CONFIG_SERIAL_8250=y
CONFIG_SERIAL_8250_CONSOLE=y
CONFIG_SYSFS=y
CONFIG_TMPFS=y
CONFIG_TMPFS_XATTR=y
CONFIG_VIRTIO_CONSOLE=y
CONFIG_VIRTIO_PCI=y

# drgn needs /proc/kcore for live debugging.
CONFIG_PROC_KCORE=y
# In some cases, it also needs /proc/kallsyms.
CONFIG_KALLSYMS=y
CONFIG_KALLSYMS_ALL=y

# drgn needs debug info.
CONFIG_DEBUG_KERNEL=y
CONFIG_DEBUG_INFO=y
CONFIG_DEBUG_INFO_DWARF4=y

# Before Linux kernel commit 8757dc970f55 ("x86/crash: Define
# arch_crash_save_vmcoreinfo() if CONFIG_CRASH_CORE=y") (in v5.6), some
# important information in VMCOREINFO is initialized by the kexec code.
CONFIG_KEXEC=y

# For block tests.
CONFIG_BLK_DEV_LOOP=m

# For kconfig tests.
CONFIG_IKCONFIG=m
CONFIG_IKCONFIG_PROC=y
"""


class KBuild:
    def __init__(self, kernel_dir: Path, build_dir: Path) -> None:
        self._build_dir = build_dir
        self._kernel_dir = kernel_dir
        self._cached_make_args: Optional[Tuple[str, ...]] = None
        self._cached_kernel_release: Optional[str] = None

    def _prepare_make(self) -> Tuple[str, ...]:
        if self._cached_make_args is None:
            self._build_dir.mkdir(parents=True, exist_ok=True)

            debug_prefix_map = []
            # GCC uses the "logical" working directory, i.e., the PWD
            # environment variable, when it can. See
            # https://gcc.gnu.org/git/?p=gcc.git;a=blob;f=libiberty/getpwd.c;hb=HEAD.
            # Map both the canonical and logical paths.
            build_dir_real = self._build_dir.resolve()
            debug_prefix_map.append(str(build_dir_real) + "=.")
            build_dir_logical = subprocess.check_output(
                f"cd {shlex.quote(str(self._build_dir))}; pwd -L",
                shell=True,
                universal_newlines=True,
            )[:-1]
            if build_dir_logical != str(build_dir_real):
                debug_prefix_map.append(build_dir_logical + "=.")

            # Before Linux kernel commit 25b146c5b8ce ("kbuild: allow Kbuild to
            # start from any directory") (in v5.2), O= forces the source
            # directory to be absolute. Since Linux kernel commit 95fd3f87bfbe
            # ("kbuild: add a flag to force absolute path for srctree") (in
            # v5.3), KBUILD_ABS_SRCTREE=1 does the same. This means that except
            # for v5.2, which we don't support, the source directory will
            # always be absolute, and we don't need to worry about mapping it
            # from a relative path.
            kernel_dir_real = self._kernel_dir.resolve()
            if kernel_dir_real != build_dir_real:
                debug_prefix_map.append(str(kernel_dir_real) + "/=./")

            cflags = " ".join(["-fdebug-prefix-map=" + map for map in debug_prefix_map])

            self._cached_make_args = (
                "-C",
                str(self._kernel_dir),
                "O=" + str(build_dir_real),
                "KBUILD_ABS_SRCTREE=1",
                "KBUILD_BUILD_USER=drgn",
                "KBUILD_BUILD_HOST=drgn",
                "KAFLAGS=" + cflags,
                "KCFLAGS=" + cflags,
                "-j",
                str(nproc()),
                "-s",
            )
        return self._cached_make_args

    def _kernel_release(self) -> str:
        if self._cached_kernel_release is None:
            if self._cached_make_args is None:
                raise ValueError("not prepared for make")
            self._cached_kernel_release = subprocess.check_output(
                ["make", *self._cached_make_args, "-s", "kernelrelease"],
                universal_newlines=True,
            ).strip()
        return self._cached_kernel_release

    def build(self) -> None:
        make_args = self._prepare_make()

        config = self._build_dir / ".config"
        tmp_config = self._build_dir / ".config.new"

        logger.info("configuring kernel")
        tmp_config.write_text(kconfig())
        subprocess.check_call(
            [
                "make",
                *make_args,
                "KCONFIG_CONFIG=" + tmp_config.name,
                "olddefconfig",
            ]
        )
        try:
            equal = filecmp.cmp(config, tmp_config)
            if not equal:
                logger.info("kernel configuration changed")
        except FileNotFoundError:
            equal = False
            logger.info("no previous kernel configuration")
        if equal:
            logger.info("kernel configuration did not change")
            tmp_config.unlink()
        else:
            tmp_config.rename(config)

        logger.info("building kernel %s", self._kernel_release())
        subprocess.check_call(["make", *make_args, "all"])
        logger.info("built kernel %s", self._kernel_release())

    def package(self, output_dir: Path, package_name: Optional[str] = None) -> Path:
        make_args = self._prepare_make()

        if package_name is None:
            package_name = f"kernel-{self._kernel_release()}.tar.zst"
        tarball = output_dir / package_name

        logger.info("packaging kernel %s to %s", self._kernel_release(), tarball)

        image_name = subprocess.check_output(
            ["make", *make_args, "-s", "image_name"], universal_newlines=True
        ).strip()

        with tempfile.TemporaryDirectory(
            prefix="install.", dir=self._build_dir
        ) as tmp_name:
            tmp_dir = Path(tmp_name)
            modules_dir = tmp_dir / "lib" / "modules" / self._kernel_release()

            logger.info("installing modules")
            subprocess.check_call(
                [
                    "make",
                    *make_args,
                    "INSTALL_MOD_PATH=" + str(tmp_dir.resolve()),
                    "modules_install",
                ]
            )
            # Don't want these symlinks.
            (modules_dir / "build").unlink()
            (modules_dir / "source").unlink()

            logger.info("copying vmlinux")
            vmlinux = modules_dir / "vmlinux"
            subprocess.check_call(
                [
                    "objcopy",
                    "--remove-relocations=*",
                    self._build_dir / "vmlinux",
                    str(vmlinux),
                ]
            )
            vmlinux.chmod(0o644)

            logger.info("copying vmlinuz")
            vmlinuz = modules_dir / "vmlinuz"
            shutil.copy(self._build_dir / image_name, vmlinuz)
            vmlinuz.chmod(0o644)

            logger.info("creating tarball")
            tarball.parent.mkdir(parents=True, exist_ok=True)
            with subprocess.Popen(
                ["tar", "-C", modules_dir, "-c", "."], stdout=subprocess.PIPE
            ) as tar_proc, subprocess.Popen(
                ["zstd", "-T0", "-19", "-q", "-", "-o", tarball, "-f"],
                stdin=tar_proc.stdout,
            ) as zstd_proc:
                tar_proc.stdout.close()  # type: ignore[union-attr]
            if tar_proc.returncode != 0 or zstd_proc.returncode != 0:
                try:
                    tarball.unlink()
                except FileNotFoundError:
                    pass
                if tar_proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        tar_proc.returncode, tar_proc.args
                    )
                if zstd_proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        zstd_proc.returncode, zstd_proc.args
                    )

        logger.info("packaged kernel %s to %s", self._kernel_release(), tarball)
        return tarball


def get_latest_releases() -> List[str]:
    ls_remote = subprocess.check_output(
        ["git", "ls-remote", "--tags", "--refs", STABLE_LINUX_GIT_URL],
        universal_newlines=True,
    )
    versions_re = "|".join(
        [version.replace(".", "\\.") for version in SUPPORTED_KERNEL_VERSIONS]
    )
    releases = re.findall(
        r"[a-f0-9]+\s+refs/tags/v(" + versions_re + ")(-rc[0-9]+|\.[0-9]+)?",
        ls_remote,
    )
    releases.sort(key=lambda release: KernelVersion(release[0] + release[1]))
    latest_releases = {}
    for release in releases:
        latest_releases[release[0]] = release[0] + release[1]
    return ["v" + release for release in reversed(latest_releases.values())]


def cmd_autobuild(args: argparse.Namespace) -> None:
    kernel_dir = Path(args.kernel_directory)
    build_dir = Path(args.build_directory)
    package_dir = Path(args.package_directory)

    logger.info("getting list of latest releases")
    releases = get_latest_releases()
    if not releases:
        logger.error("no releases found")
        sys.exit(1)
    logger.info("latest releases are: %r", releases)

    if not kernel_dir.exists():
        logger.info("creating repository")
        subprocess.check_call(
            [
                "git",
                "init",
                kernel_dir,
            ]
        )

    logger.info("fetching releases")
    subprocess.check_call(
        [
            "git",
            "-C",
            str(kernel_dir),
            "fetch",
            "--depth",
            "1",
            STABLE_LINUX_GIT_URL,
        ]
        + [f"refs/tags/{tag}:refs/tags/{tag}" for tag in releases]
    )

    for release in releases:
        release_build_dir = build_dir / ("build-" + release)
        logger.info("preparing to build %s in %s", release, release_build_dir)
        subprocess.check_call(["git", "-C", kernel_dir, "checkout", release])
        kbuild = KBuild(kernel_dir, release_build_dir)
        kbuild.build()
        kbuild.package(package_dir)


def cmd_build(args: argparse.Namespace) -> None:
    kbuild = KBuild(Path(args.kernel_directory), Path(args.build_directory))
    kbuild.build()
    if hasattr(args, "package"):
        kbuild.package(Path(args.package))


def cmd_config(args: argparse.Namespace) -> None:
    sys.stdout.write(kconfig())


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s:%(levelname)s:%(name)s:%(message)s", level=logging.INFO
    )
    parser = argparse.ArgumentParser(description="Build/package drgn vmtest kernels")

    subparsers = parser.add_subparsers(
        title="command", description="command to run", dest="command"
    )
    subparsers.required = True

    parser_autobuild = subparsers.add_parser(
        "autobuild",
        help="build and package the latest supported kernel releases",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_autobuild.add_argument(
        "-k",
        "--kernel-directory",
        type=str,
        metavar="DIR",
        help="kernel Git repository directory (created if needed)",
        default=".",
    )
    parser_autobuild.add_argument(
        "-b",
        "--build-directory",
        type=str,
        metavar="DIR",
        help="parent directory for kernel build directories (one per release)",
        default=".",
    )
    parser_autobuild.add_argument(
        "-p",
        "--package-directory",
        type=str,
        metavar="DIR",
        help="output directory for packaged kernels",
        default=".",
    )
    parser_autobuild.set_defaults(func=cmd_autobuild)

    parser_build = subparsers.add_parser(
        "build",
        help="build a drgn vmtest kernel",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_build.add_argument(
        "-k",
        "--kernel-directory",
        type=str,
        metavar="DIR",
        help="kernel source tree directory",
        default=".",
    )
    parser_build.add_argument(
        "-b",
        "--build-directory",
        type=str,
        metavar="DIR",
        help="build output directory",
        default=".",
    )
    parser_build.add_argument(
        "-p",
        "--package",
        type=str,
        metavar="DIR",
        help="also package the built kernel and place it in DIR",
        default=argparse.SUPPRESS,
    )
    parser_build.set_defaults(func=cmd_build)

    parser_config = subparsers.add_parser(
        "config",
        help="print the drgn vmtest kernel configuration to standard output",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_config.set_defaults(func=cmd_config)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
