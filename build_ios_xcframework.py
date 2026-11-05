#!/usr/bin/env python3
"""
build_ios_xcframework.py
A fully self-contained Python tool to build and package whisper.cpp into an Apple XCFramework.
- Automatically compiles iOS Device, iOS Simulator (universal arm64/x86_64 for Intel & Apple Silicon), and macOS.
- If 'cmake' is missing on the machine, automatically downloads a portable standalone universal CMake binary (no root/brew required).
- Packages universal XCFramework with zero configuration.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

# Headers required by the whisper framework
FRAMEWORK_HEADERS = [
    "include/whisper.h",
    "include/parakeet.h",
    "ggml/include/ggml.h",
    "ggml/include/ggml-alloc.h",
    "ggml/include/ggml-backend.h",
    "ggml/include/ggml-metal.h",
    "ggml/include/ggml-cpu.h",
    "ggml/include/ggml-blas.h",
    "ggml/include/gguf.h",
]

MODULE_MAP_CONTENT = """framework module whisper {
    header "whisper.h"
    header "parakeet.h"
    header "ggml.h"
    header "ggml-alloc.h"
    header "ggml-backend.h"
    header "ggml-metal.h"
    header "ggml-cpu.h"
    header "ggml-blas.h"
    header "gguf.h"

    link "c++"
    link framework "Accelerate"
    link framework "Metal"
    link framework "Foundation"
    link framework "CoreML"

    export *
}
"""

def get_sdk_path(sdk: str) -> str:
    res = subprocess.run(["xcrun", "--sdk", sdk, "--show-sdk-path"], capture_output=True, text=True, check=True)
    return res.stdout.strip()

def ensure_cmake() -> str:
    """Finds system cmake or automatically downloads a standalone universal macOS CMake binary."""
    cmake = shutil.which("cmake")
    if cmake:
        return cmake

    cache_dir = Path.home() / ".cache" / "py_cmake"
    for p in cache_dir.rglob("bin/cmake"):
        if os.access(p, os.X_OK):
            return str(p)

    print("\n[PyCMake] 'cmake' was not found on this machine.")
    print("Downloading official standalone universal macOS CMake (no sudo/brew needed)...")
    url = "https://github.com/Kitware/CMake/releases/download/v3.30.5/cmake-3.30.5-macos-universal.tar.gz"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tar_path = cache_dir / "cmake.tar.gz"

    try:
        print(f"Fetching {url} ...")
        urllib.request.urlretrieve(url, tar_path)
        print("Extracting portable CMake...")
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(cache_dir)
        tar_path.unlink(missing_ok=True)

        for p in cache_dir.rglob("bin/cmake"):
            if os.access(p, os.X_OK):
                print(f"Portable CMake ready: {p}\n")
                return str(p)
    except Exception as e:
        print(f"Automatic CMake download failed: {e}")
        print("Please install cmake (e.g. brew install cmake) or install CMake from cmake.org.")
        sys.exit(1)

    return "cmake"

def compile_slice(cmake_bin: str, root_dir: Path, build_dir: Path, platform_type: str):
    """Compiles an Apple target slice using CMake + Xcode generator."""
    if build_dir.exists() and any(build_dir.rglob("*.a")):
        print(f"[{platform_type}] Found pre-compiled libraries in {build_dir.name}. Skipping compilation.")
        return

    print(f"\n[{platform_type}] Compiling whisper.cpp from source for {platform_type}...")
    common_args = [
        "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_REQUIRED=NO",
        "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY=",
        "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED=NO",
        "-DWHISPER_BUILD_EXAMPLES=OFF",
        "-DWHISPER_BUILD_TESTS=OFF",
        "-DWHISPER_BUILD_SERVER=OFF",
        "-DGGML_METAL=ON",
        "-DGGML_METAL_EMBED_LIBRARY=ON",
        "-DGGML_BLAS_DEFAULT=ON",
        "-DGGML_METAL_USE_BF16=ON",
        "-DGGML_NATIVE=OFF",
        "-DGGML_OPENMP=OFF",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DWHISPER_COREML=ON",
        "-DWHISPER_COREML_ALLOW_FALLBACK=ON",
    ]

    if platform_type == "ios":
        args = [
            cmake_bin, "-B", str(build_dir), "-G", "Xcode",
            "-DCMAKE_SYSTEM_NAME=iOS",
            "-DCMAKE_OSX_SYSROOT=iphoneos",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=16.4",
            "-DCMAKE_XCODE_ATTRIBUTE_SUPPORTED_PLATFORMS=iphoneos",
            *common_args,
            "-S", str(root_dir),
        ]
    elif platform_type == "ios-sim":
        args = [
            cmake_bin, "-B", str(build_dir), "-G", "Xcode",
            "-DCMAKE_SYSTEM_NAME=iOS",
            "-DCMAKE_OSX_SYSROOT=iphonesimulator",
            "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64",
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=16.4",
            "-DCMAKE_XCODE_ATTRIBUTE_SUPPORTED_PLATFORMS=iphonesimulator",
            *common_args,
            "-S", str(root_dir),
        ]
    elif platform_type == "macos":
        args = [
            cmake_bin, "-B", str(build_dir), "-G", "Xcode",
            "-DCMAKE_OSX_SYSROOT=macosx",
            "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64",
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.3",
            *common_args,
            "-S", str(root_dir),
        ]
    else:
        return

    print(f"[{platform_type}] Configuring Xcode project...")
    subprocess.run(args, check=True)

    print(f"[{platform_type}] Building Release binaries...")
    subprocess.run([cmake_bin, "--build", str(build_dir), "--config", "Release", "--", "-quiet"], check=True)
    print(f"[{platform_type}] ✓ Compilation successful!\n")

def setup_framework_structure(build_dir: Path, platform_type: str, min_os: str = "16.4", root_dir: Path = None):
    root_dir = root_dir or Path.cwd()
    fw_name = "whisper"
    fw_dir = build_dir / "framework" / f"{fw_name}.framework"

    if platform_type == "macos":
        versions_a = fw_dir / "Versions" / "A"
        headers_dir = versions_a / "Headers"
        modules_dir = versions_a / "Modules"
        resources_dir = versions_a / "Resources"
        headers_dir.mkdir(parents=True, exist_ok=True)
        modules_dir.mkdir(parents=True, exist_ok=True)
        resources_dir.mkdir(parents=True, exist_ok=True)

        current_sym = fw_dir / "Versions" / "Current"
        if not current_sym.exists():
            os.symlink("A", current_sym)
        for d in ["Headers", "Modules", "Resources", fw_name]:
            target = fw_dir / d
            if not target.exists() and not target.is_symlink():
                os.symlink(f"Versions/Current/{d}", target)
        plist_path = resources_dir / "Info.plist"
        binary_path = versions_a / fw_name
    else:
        headers_dir = fw_dir / "Headers"
        modules_dir = fw_dir / "Modules"
        headers_dir.mkdir(parents=True, exist_ok=True)
        modules_dir.mkdir(parents=True, exist_ok=True)
        plist_path = fw_dir / "Info.plist"
        binary_path = fw_dir / fw_name

    for h in FRAMEWORK_HEADERS:
        src_h = root_dir / h
        if src_h.exists():
            shutil.copy2(src_h, headers_dir / src_h.name)

    (modules_dir / "module.modulemap").write_text(MODULE_MAP_CONTENT)

    supported_platform = "iPhoneOS" if platform_type == "ios" else ("iPhoneSimulator" if platform_type == "ios-sim" else "MacOSX")
    dt_platform = "iphoneos" if platform_type == "ios" else ("iphonesimulator" if platform_type == "ios-sim" else "macosx")
    device_family = """    <key>UIDeviceFamily</key>
    <array>
        <integer>1</integer>
        <integer>2</integer>
    </array>""" if platform_type != "macos" else ""

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>whisper</string>
    <key>CFBundleIdentifier</key>
    <string>org.ggml.whisper</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>whisper</string>
    <key>CFBundlePackageType</key>
    <string>FMWK</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>MinimumOSVersion</key>
    <string>{min_os}</string>
    <key>CFBundleSupportedPlatforms</key>
    <array>
        <string>{supported_platform}</string>
    </array>
{device_family}
    <key>DTPlatformName</key>
    <string>{dt_platform}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)
    return fw_dir, binary_path

def combine_and_link(build_dir: Path, release_subpath: str, platform_type: str, binary_path: Path, static: bool = True):
    libs = [
        build_dir / f"src/{release_subpath}/libwhisper.a",
        build_dir / f"src/{release_subpath}/libparakeet.a",
        build_dir / f"ggml/src/{release_subpath}/libggml.a",
        build_dir / f"ggml/src/{release_subpath}/libggml-base.a",
        build_dir / f"ggml/src/{release_subpath}/libggml-cpu.a",
        build_dir / f"ggml/src/ggml-metal/{release_subpath}/libggml-metal.a",
        build_dir / f"ggml/src/ggml-blas/{release_subpath}/libggml-blas.a",
        build_dir / f"src/{release_subpath}/libwhisper.coreml.a",
    ]
    existing_libs = [l for l in libs if l.exists()]
    if not existing_libs:
        existing_libs = [l for l in build_dir.rglob("*.a") if "CompilerId" not in str(l)]

    print(f"[{platform_type}] Combining {len(existing_libs)} static libraries...")
    temp_dir = build_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    combined_a = temp_dir / "combined.a"

    cmd = ["libtool", "-static", "-o", str(combined_a)] + [str(l) for l in existing_libs]
    subprocess.run(cmd, check=True, stderr=subprocess.DEVNULL)

    if static:
        print(f"[{platform_type}] Installing static binary -> {binary_path}")
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(combined_a, binary_path)
    else:
        sdk = "iphoneos" if platform_type == "ios" else ("iphonesimulator" if platform_type == "ios-sim" else "macosx")
        sdk_path = get_sdk_path(sdk)
        min_ver = "-miphoneos-version-min=16.4" if platform_type == "ios" else ("-mios-simulator-version-min=16.4" if platform_type == "ios-sim" else "-mmacosx-version-min=13.3")
        install_name = "@rpath/whisper.framework/whisper" if platform_type != "macos" else "@rpath/whisper.framework/Versions/Current/whisper"

        arch_args = ["-arch", "arm64"]
        if platform_type in ["ios-sim", "macos"]:
            # Universal for Apple Silicon & Intel Mac
            arch_args.extend(["-arch", "x86_64"])

        print(f"[{platform_type}] Linking dynamic library -> {binary_path}")
        link_cmd = [
            "xcrun", "-sdk", sdk, "clang++", "-dynamiclib",
            "-isysroot", sdk_path,
            *arch_args,
            min_ver,
            "-Wl,-force_load," + str(combined_a),
            "-framework", "Foundation",
            "-framework", "Metal",
            "-framework", "Accelerate",
            "-framework", "CoreML",
            "-install_name", install_name,
            "-o", str(binary_path),
        ]
        subprocess.run(link_cmd, check=True)

        if platform_type == "ios":
            try:
                subprocess.run(["xcrun", "vtool", "-set-build-version", "ios", "16.4", "16.4", "-replace", "-output", str(binary_path), str(binary_path)], check=True)
            except Exception:
                pass

    shutil.rmtree(temp_dir, ignore_errors=True)

def build_xcframework(root_dir: Path, output_path: Path, static: bool = True, include_macos: bool = True):
    print("=== Auto-Building & Packaging Apple XCFramework in Python ===\n")
    cmake_bin = None
    framework_dirs = []

    # 1. iOS Device (arm64)
    ios_device_dir = root_dir / "build-ios-device"
    if not (ios_device_dir.exists() and any(ios_device_dir.rglob("*.a"))):
        if not cmake_bin:
            cmake_bin = ensure_cmake()
        compile_slice(cmake_bin, root_dir, ios_device_dir, "ios")

    fw, bin_p = setup_framework_structure(ios_device_dir, "ios", min_os="16.4", root_dir=root_dir)
    combine_and_link(ios_device_dir, "Release-iphoneos", "ios", bin_p, static=static)
    framework_dirs.append(fw)

    # 2. iOS Simulator (arm64 + x86_64 universal)
    ios_sim_dir = root_dir / "build-ios-sim"
    if not (ios_sim_dir.exists() and any(ios_sim_dir.rglob("*.a"))):
        if not cmake_bin:
            cmake_bin = ensure_cmake()
        compile_slice(cmake_bin, root_dir, ios_sim_dir, "ios-sim")

    fw, bin_p = setup_framework_structure(ios_sim_dir, "ios-sim", min_os="16.4", root_dir=root_dir)
    combine_and_link(ios_sim_dir, "Release-iphonesimulator", "ios-sim", bin_p, static=static)
    framework_dirs.append(fw)

    # 3. macOS (arm64 + x86_64 universal, optional)
    if include_macos:
        macos_dir = root_dir / "build-macos"
        if not (macos_dir.exists() and any(macos_dir.rglob("*.a"))):
            if not cmake_bin:
                cmake_bin = ensure_cmake()
            compile_slice(cmake_bin, root_dir, macos_dir, "macos")

        fw, bin_p = setup_framework_structure(macos_dir, "macos", min_os="13.3", root_dir=root_dir)
        combine_and_link(macos_dir, "Release", "macos", bin_p, static=static)
        framework_dirs.append(fw)

    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nRunning xcodebuild -create-xcframework with {len(framework_dirs)} slices...")
    xc_cmd = ["xcodebuild", "-create-xcframework"]
    for f in framework_dirs:
        xc_cmd.extend(["-framework", str(f)])
    xc_cmd.extend(["-output", str(output_path)])

    subprocess.run(xc_cmd, check=True)
    print(f"\n\033[1;32m✓ Successfully generated XCFramework:\033[0m")
    print(f"  {output_path}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build and package whisper.cpp into an XCFramework.")
    parser.add_argument("--dynamic", action="store_true", help="Build dynamic framework instead of static")
    parser.add_argument("--output", default="build-apple/whisper.xcframework", help="Output path for .xcframework")
    parser.add_argument("--no-macos", action="store_true", help="Omit macOS slice (iOS only)")
    args = parser.parse_args()

    whisper_dir = Path.cwd() / "whisper.cpp" if (Path.cwd() / "whisper.cpp").exists() else Path.cwd()
    out = (whisper_dir / args.output).resolve()
    build_xcframework(whisper_dir, out, static=not args.dynamic, include_macos=not args.no_macos)
