#!/usr/bin/env python3
"""
build_ios_xcframework.py
A pure-Python tool to build and package whisper.cpp into an Apple XCFramework.
No CMake required! Uses standard Apple command line tools (clang, libtool, xcodebuild).
"""

import argparse
import os
import shutil
import subprocess
import sys
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

def setup_framework_structure(build_dir: Path, platform_type: str, min_os: str = "16.0", root_dir: Path = None):
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

        # Symlinks
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
        # iOS flat structure
        headers_dir = fw_dir / "Headers"
        modules_dir = fw_dir / "Modules"
        headers_dir.mkdir(parents=True, exist_ok=True)
        modules_dir.mkdir(parents=True, exist_ok=True)
        plist_path = fw_dir / "Info.plist"
        binary_path = fw_dir / fw_name

    # Copy headers
    for h in FRAMEWORK_HEADERS:
        src_h = root_dir / h
        if src_h.exists():
            shutil.copy2(src_h, headers_dir / src_h.name)

    # Write module map
    (modules_dir / "module.modulemap").write_text(MODULE_MAP_CONTENT)

    # Info.plist
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
    # Find all static libraries (.a)
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
        # Fallback search if path structure varies
        existing_libs = list(build_dir.rglob("*.a"))
        # Filter out CMake compiler check test libs
        existing_libs = [l for l in existing_libs if "CompilerId" not in str(l)]

    print(f"[{platform_type}] Combining {len(existing_libs)} static libraries...")
    temp_dir = build_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    combined_a = temp_dir / "combined.a"

    # Combine using Apple libtool
    cmd = ["libtool", "-static", "-o", str(combined_a)] + [str(l) for l in existing_libs]
    subprocess.run(cmd, check=True, stderr=subprocess.DEVNULL)

    if static:
        print(f"[{platform_type}] Installing combined static binary -> {binary_path}")
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(combined_a, binary_path)
    else:
        # Create dynamic library
        sdk = "iphoneos" if platform_type == "ios" else ("iphonesimulator" if platform_type == "ios-sim" else "macosx")
        sdk_path = get_sdk_path(sdk)
        min_ver = "-miphoneos-version-min=16.0" if platform_type == "ios" else ("-mios-simulator-version-min=16.0" if platform_type == "ios-sim" else "-mmacosx-version-min=13.0")
        install_name = "@rpath/whisper.framework/whisper" if platform_type != "macos" else "@rpath/whisper.framework/Versions/Current/whisper"

        print(f"[{platform_type}] Linking dynamic library -> {binary_path}")
        link_cmd = [
            "xcrun", "-sdk", sdk, "clang++", "-dynamiclib",
            "-isysroot", sdk_path,
            "-arch", "arm64",
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
            # Tag iOS build version if vtool exists
            try:
                subprocess.run(["xcrun", "vtool", "-set-build-version", "ios", "16.0", "16.0", "-replace", "-output", str(binary_path), str(binary_path)], check=True)
            except Exception:
                pass

    shutil.rmtree(temp_dir, ignore_errors=True)

def build_xcframework(root_dir: Path, output_path: Path, static: bool = True, include_macos: bool = True):
    print("=== Packaging Apple XCFramework in Pure Python (No CMake required) ===\n")
    framework_dirs = []

    # 1. iOS Device
    ios_device_dir = root_dir / "build-ios-device"
    if ios_device_dir.exists():
        fw, bin_p = setup_framework_structure(ios_device_dir, "ios", min_os="16.4", root_dir=root_dir)
        combine_and_link(ios_device_dir, "Release-iphoneos", "ios", bin_p, static=static)
        framework_dirs.append(fw)

    # 2. iOS Simulator
    ios_sim_dir = root_dir / "build-ios-sim"
    if ios_sim_dir.exists():
        fw, bin_p = setup_framework_structure(ios_sim_dir, "ios-sim", min_os="16.4", root_dir=root_dir)
        combine_and_link(ios_sim_dir, "Release-iphonesimulator", "ios-sim", bin_p, static=static)
        framework_dirs.append(fw)

    # 3. macOS (optional)
    macos_dir = root_dir / "build-macos"
    if include_macos and macos_dir.exists():
        fw, bin_p = setup_framework_structure(macos_dir, "macos", min_os="13.3", root_dir=root_dir)
        combine_and_link(macos_dir, "Release", "macos", bin_p, static=static)
        framework_dirs.append(fw)

    if not framework_dirs:
        print("Error: No framework builds found!")
        sys.exit(1)

    # Clean existing xcframework
    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nRunning xcodebuild -create-xcframework with {len(framework_dirs)} slices...")
    xc_cmd = ["xcodebuild", "-create-xcframework"]
    for fw in framework_dirs:
        xc_cmd.extend(["-framework", str(fw)])
    xc_cmd.extend(["-output", str(output_path)])

    subprocess.run(xc_cmd, check=True)
    print(f"\n\033[1;32m✓ Successfully generated XCFramework:\033[0m")
    print(f"  {output_path}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package whisper.cpp into an XCFramework without CMake.")
    parser.add_argument("--dynamic", action="store_true", help="Build dynamic framework instead of static")
    parser.add_argument("--output", default="build-apple/whisper.xcframework", help="Output path for .xcframework")
    parser.add_argument("--no-macos", action="store_true", help="Omit macOS slice (iOS only)")
    args = parser.parse_args()

    whisper_dir = Path.cwd() / "whisper.cpp" if (Path.cwd() / "whisper.cpp").exists() else Path.cwd()
    out = (whisper_dir / args.output).resolve()
    build_xcframework(whisper_dir, out, static=not args.dynamic, include_macos=not args.no_macos)
