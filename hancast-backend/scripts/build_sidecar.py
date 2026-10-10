"""Nuitka 构建脚本 - 将 Python Sidecar 编译为 standalone 目录结构"""

import os
import sys
import platform
import subprocess
import shutil


def get_target_triple():
    """获取当前平台的 Rust target triple（与 Tauri externalBin 约定一致）"""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine == "arm64":
            return "aarch64-pc-windows-msvc"
        return "x86_64-pc-windows-msvc"
    elif system == "darwin":
        if machine == "arm64":
            return "aarch64-apple-darwin"
        return "x86_64-apple-darwin"
    elif system == "linux":
        if machine == "aarch64":
            return "aarch64-unknown-linux-gnu"
        return "x86_64-unknown-linux-gnu"
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def copy_icon(src_tauri_dir):
    """复制 HanCast-air.png 图标到 src-tauri/icons/ 目录"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(current_dir)
    png_dir = os.path.join(os.path.dirname(project_dir), "png")
    icon_src = os.path.join(png_dir, "HanCast-air.png")
    icons_dir = os.path.join(src_tauri_dir, "icons")

    if os.path.exists(icon_src):
        os.makedirs(icons_dir, exist_ok=True)
        icon_dst = os.path.join(icons_dir, "icon.png")
        shutil.copy2(icon_src, icon_dst)
        print(f"Copied icon: {icon_src} -> {icon_dst}")

        icon_128_2x_dst = os.path.join(icons_dir, "128x128@2x.png")
        shutil.copy2(icon_src, icon_128_2x_dst)
        print(f"Copied icon: {icon_src} -> {icon_128_2x_dst}")
    else:
        print(f"Warning: Icon not found at {icon_src}")



def flatten_dependencies(src_tauri_dir, output_name):
    """把 Nuitka 产出的运行时依赖从 hancast-sidecar/ 提升到 src-tauri/ 根目录。

    Tauri 的 externalBin 只把 sidecar 可执行文件搬到安装目录根部，而
    bundle.resources 中的 `*.dll` / `*.pyd` / `certifi/**/*` / `lxml/**/*` /
    `charset_normalizer/**/*` 等 glob 都是相对 src-tauri/ 根目录解析的。
    两者布局不一致会导致：
      1) glob 匹配不到任何文件 —— build.rs 直接报
         "glob pattern ... path not found or didn't match any files"，构建失败；
      2) 即便构建通过，运行时 sidecar 也找不到与自身同级的动态库。

    这里把 dist 目录里的依赖递归合并到 src-tauri/ 根，使其与
    hancast_sidecar/utils/mpv_manager.py 中描述的安装目录结构一致：

        {install_dir}/
        ├── hancast-sidecar(.exe)     ← 由 externalBin 提供
        ├── HanCast(.exe)
        ├── mpv/
        ├── hancast_sidecar/         ← Python 数据文件 (xml/ 等)
        └── *.dll / *.pyd / *.so / *.dylib, certifi/, lxml/ ...

    注意：sidecar 可执行文件本身不在此复制，避免与 externalBin 的产物重复。
    """
    dist_dir = os.path.join(src_tauri_dir, "hancast-sidecar")
    if not os.path.isdir(dist_dir):
        print(f"Warning: {dist_dir} not found, skip flattening dependencies")
        return

    ext = ".exe" if sys.platform == "win32" else ""
    skip_names = {f"{output_name}{ext}"}

    # 必须在复制之前改：这样 src-tauri/hancast-sidecar/ 与 src-tauri/ 根目录
    # 两份 dylib 的 install name 都是 @rpath，不会出现只改一半的情况。
    fix_macos_dylib_paths(src_tauri_dir)

    copied = 0
    for name in os.listdir(dist_dir):
        if name in skip_names:
            continue
        src = os.path.join(dist_dir, name)
        dst = os.path.join(src_tauri_dir, name)
        try:
            if os.path.isdir(src):
                # 目录递归合并：hancast_sidecar/ 等目录可能已由 sync-sidecar.cjs 建立
                shutil.copytree(src, dst, dirs_exist_ok=True)
                kind = "dir "
            else:
                shutil.copy2(src, dst)
                kind = "file"
            copied += 1
            print(f"  flatten {kind}  {name}")
        except (shutil.Error, OSError) as exc:
            print(f"Warning: failed to flatten {name}: {exc}")

    if copied:
        print(f"Flattened {copied} dependency entries: {dist_dir} -> {src_tauri_dir}")


def fix_macos_dylib_paths(src_tauri_dir):
    """把 libpython*.dylib 的加载路径改成 @rpath，并补上指向 Resources/ 的 rpath。

    背景（macOS 打包的坑，实测确认）：
      Nuitka 产出的 hancast-sidecar 通过
          @executable_path/libpython3.12.dylib
      引用 Python 运行时，即「和可执行文件同级」。

      而 Tauri 打包时两者落点不同：
        externalBin  →  HanCast.app/Contents/MacOS/hancast-sidecar
        resources    →  HanCast.app/Contents/Resources/*.so, hancast_sidecar/, ...
      于是 @executable_path 指向 Contents/MacOS/，dylib 却在 Contents/Resources/，
      运行时 dyld 报：
          Library not loaded: @executable_path/libpython3.12.dylib
      sidecar 进程直接退出 → 设备发现 / 投屏全部失效（用户表现为「发现不了设备」）。

    修法（已在真机上验证三种布局都能加载）：
      1) dylib 的 install name 改成 @rpath/libpython3.12.dylib
      2) sidecar 里对该 dylib 的引用同样改成 @rpath/...
      3) sidecar 补一条 rpath 指向 .app 内的 Resources/
    这样无论 dylib 落在 sidecar 同级还是 Contents/Resources/，dyld 都能找到。

    注意：@loader_path 在这里**不行**——作为依赖路径时它只解析成引用方所在目录，
    不会去 Resources/ 找；只有 @rpath + rpath 才有多目录回退能力。

    详见 install_name_tool(1) / dyld(1)。
    """
    if sys.platform != "darwin":
        return

    sidecar_dir = os.path.join(src_tauri_dir, "hancast-sidecar")
    if not os.path.isdir(sidecar_dir):
        return

    if shutil.which("install_name_tool") is None:
        print("Warning: install_name_tool not found, skip dylib path rewrite")
        return

    dylibs = [n for n in os.listdir(sidecar_dir) if n.endswith(".dylib")]
    if not dylibs:
        print("No .dylib found, skip install_name rewrite")
        return

    exes = [
        n for n in os.listdir(sidecar_dir)
        if not n.endswith(".dylib")
        and os.path.isfile(os.path.join(sidecar_dir, n))
        and os.access(os.path.join(sidecar_dir, n), os.X_OK)
    ]

    for dylib in dylibs:
        dylib_path = os.path.join(sidecar_dir, dylib)
        rpath_ref = f"@rpath/{dylib}"

        # 1) dylib 自身的 install name
        r = subprocess.run(
            ["install_name_tool", "-id", rpath_ref, dylib_path],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"Warning: failed to set install name of {dylib}: {r.stderr.strip()}")
            continue
        print(f"  install_name  {dylib} -> {rpath_ref}")

        # 2) 每个可执行文件里的引用
        for exe in exes:
            exe_path = os.path.join(sidecar_dir, exe)
            r2 = subprocess.run(
                ["install_name_tool", "-change",
                 f"@executable_path/{dylib}", rpath_ref, exe_path],
                capture_output=True, text=True,
            )
            if r2.returncode == 0:
                print(f"  changed ref   {exe}: @executable_path -> {rpath_ref}")

    # 3) 给可执行文件补 rpath（两条，覆盖两种可能布局）
    #
    #   @loader_path                  → dylib 与 sidecar 同级（externalBin 风格）
    #   @loader_path/../Resources     → dylib 在 .app 的 Resources/ 里
    #
    # 「改」已有的 @executable_path 那条（Nuitka 生成的，语义已失效）而不是
    # 盲目 -add_rpath，避免 headerpad 不足导致的失败——真实踩过的坑：
    #   changing install names or rpaths can't be redone ... larger updated
    #   load commands do not fit (the program must be relinked, and you may
    #   need to use -headerpad or -headerpad_max_install_names)
    targets = ["@loader_path", "@loader_path/../Resources"]

    for exe in exes:
        exe_path = os.path.join(sidecar_dir, exe)

        def current_rpaths():
            out = subprocess.run(
                ["otool", "-l", exe_path], capture_output=True, text=True,
            ).stdout
            return [
                line.split("path ", 1)[1].split(" (offset", 1)[0].strip()
                for line in out.splitlines()
                if line.strip().startswith("path ") and "(offset" in line
            ]

        # @executable_path 对 sidecar 来说是错误目录，优先回收它的槽位
        for target in targets:
            old_rpaths_now = current_rpaths()
            if target in old_rpaths_now:
                continue
            victim = "@executable_path" if "@executable_path" in old_rpaths_now else None
            if victim is not None:
                r = subprocess.run(
                    ["install_name_tool", "-rpath", victim, target, exe_path],
                    capture_output=True, text=True,
                )
            else:
                r = subprocess.run(
                    ["install_name_tool", "-add_rpath", target, exe_path],
                    capture_output=True, text=True,
                )
            if r.returncode == 0:
                print(f"  rpath         {exe}: +{target}")
            else:
                print(f"  Warning: rpath {target} failed for {exe}: {r.stderr.strip()}")

        print(f"  rpaths final  {exe}: {current_rpaths()}")


def build(force=False):
    """
    构建 Sidecar 到 src-tauri/hancast-sidecar/ 子目录

    Tauri 打包时会把 exe 和依赖分别复制到资源根目录。

    输出结构:
        src-tauri/hancast-sidecar/
        ├── hancast-sidecar-{triple}.exe   ← 入口（externalBin）
        ├── *.dll / *.pyd / *.so           ← Python 扩展模块等依赖
        └── hancast_sidecar/
            └── xml/                        ← 运行时数据（UPnP 描述文件）

    注意：Nuitka standalone 会把几乎所有依赖静态并入可执行文件，
    产出目录里通常【没有】certifi/ lxml/ charset_normalizer/ 这类独立目录。
    具体产物以构建时 flatten_dependencies() 打印的清单为准。
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(current_dir)
    src_tauri_dir = os.path.join(os.path.dirname(project_dir), "src-tauri")

    output_dir = os.path.join(src_tauri_dir, "hancast-sidecar")
    target_triple = get_target_triple()

    output_name = f"hancast-sidecar-{target_triple}"
    entry_point = os.path.join(project_dir, "hancast_sidecar", "main.py")
    xml_dir = os.path.join(project_dir, "hancast_sidecar", "xml")

    ext = ".exe" if sys.platform == "win32" else ""
    output_exe = os.path.join(output_dir, f"{output_name}{ext}")

    # 已存在则跳过（--force 强制重建）
    if not force and os.path.exists(output_exe):
        size_mb = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, filenames in os.walk(output_dir)
            for f in filenames
        ) / (1024 * 1024)
        print(f"Sidecar already exists: {output_dir} ({size_mb:.1f} MB)")
        print("Use --force to rebuild")
        # 依赖仍需提升到 src-tauri/ 根目录（可能被手工清理过）
        flatten_dependencies(src_tauri_dir, output_name)
        return

    # 清理旧的构建产物
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Nuitka 输出到临时目录
    nuitka_build_dir = os.path.join(project_dir, "build")
    nuitka_dist_dir = os.path.join(nuitka_build_dir, "main.dist")

    # 查找图标文件（只支持 ICO 格式）
    icon_path = None
    src_tauri_icons = os.path.join(src_tauri_dir, "icons")
    ico_path = os.path.join(src_tauri_icons, "icon.ico")

    if os.path.exists(ico_path):
        icon_path = ico_path

    args = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        f"--output-filename={output_name}",
        f"--output-dir={nuitka_build_dir}",
        f"--include-data-dir={xml_dir}=hancast_sidecar/xml",
        "--include-package=hancast_sidecar",
        "--include-package=lxml",
        "--include-package=netifaces",
        "--nofollow-import-to=tkinter,unittest,test,distutils,setuptools,pip,_pytest,pytest",
        "--enable-plugin=anti-bloat",
        "--no-progress",
        "--assume-yes-for-downloads",
    ]

    # 添加图标参数（仅支持 ICO 格式）
    if icon_path and sys.platform == "win32":
        args.append(f"--windows-icon-from-ico={icon_path}")
        print(f"Using icon: {icon_path}")

    args.append(entry_point)

    print(f"Building sidecar: {output_name}")
    print(f"Entry point: {entry_point}")
    print(f"Output dir:  {output_dir}")
    print(f"Target:      {target_triple}")
    print()

    result = subprocess.run(args, cwd=project_dir)
    if result.returncode != 0:
        print(f"\nBuild failed with exit code {result.returncode}")
        sys.exit(1)

    # 将 Nuitka 输出移动到子目录
    if os.path.exists(nuitka_dist_dir):
        for item in os.listdir(nuitka_dist_dir):
            src = os.path.join(nuitka_dist_dir, item)
            dst = os.path.join(output_dir, item)
            shutil.move(src, dst)

        # 清理 Nuitka 临时构建目录
        shutil.rmtree(nuitka_build_dir, ignore_errors=True)

        # 删除 types/ 目录（避免与 Python 内置模块冲突）
        types_dir = os.path.join(output_dir, "types")
        if os.path.isdir(types_dir):
            shutil.rmtree(types_dir)
            print("Removed types/ directory (conflicts with built-in types module)")
    else:
        print(f"\nWarning: Nuitka output not found at {nuitka_dist_dir}")
        sys.exit(1)

    # 统计输出
    total_size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, filenames in os.walk(output_dir)
        for f in filenames
    ) / (1024 * 1024)
    file_count = sum(
        1 for _, _, filenames in os.walk(output_dir) for _ in filenames
    )

    print(f"\nBuild completed: {output_dir}")
    print(f"Total size: {total_size:.1f} MB ({file_count} files)")

    # 复制图标
    copy_icon(src_tauri_dir)

    # 将运行时依赖提升到 src-tauri/ 根目录，供 bundle.resources 打包
    flatten_dependencies(src_tauri_dir, output_name)

if __name__ == "__main__":
    force = "--force" in sys.argv
    build(force=force)
