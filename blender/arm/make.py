import errno
import glob
import json
import os
from queue import Queue
import shlex
import shutil
import stat
import subprocess
import threading
import time
from typing import Callable
import webbrowser

import bpy

import arm.assets as assets
from arm.exporter import ArmoryExporter
import arm.lib.make_datas
import arm.lib.server
import arm.live_patch as live_patch
import arm.log as log
import arm.make_logic as make_logic
import arm.make_renderpath as make_renderpath
import arm.make_state as state
import arm.make_world as make_world
import arm.utils
import arm.write_data as write_data

if arm.is_reload(__name__):
    assets = arm.reload_module(assets)
    arm.exporter = arm.reload_module(arm.exporter)
    from arm.exporter import ArmoryExporter
    arm.lib.make_datas = arm.reload_module(arm.lib.make_datas)
    arm.lib.server = arm.reload_module(arm.lib.server)
    live_patch = arm.reload_module(live_patch)
    log = arm.reload_module(log)
    make_logic = arm.reload_module(make_logic)
    make_renderpath = arm.reload_module(make_renderpath)
    state = arm.reload_module(state)
    make_world = arm.reload_module(make_world)
    arm.utils = arm.reload_module(arm.utils)
    write_data = arm.reload_module(write_data)
else:
    arm.enable_reload(__name__)

scripts_mtime = 0 # Monitor source changes
profile_time = 0

# Queue of threads and their done callbacks. Item format: [thread, done]
thread_callback_queue = Queue(maxsize=0)


def run_proc(cmd, done: Callable) -> subprocess.Popen:
    """Creates a subprocess with the given command and returns it.

    If Blender is not running in background mode, a thread is spawned
    that waits until the subprocess has finished executing to not freeze
    the UI, otherwise (in background mode) execution is blocked until
    the subprocess has finished.

    If `done` is not `None`, it is called afterwards in the main thread.
    """
    use_thread = not bpy.app.background

    def wait_for_proc(proc: subprocess.Popen):
        proc.wait()

        if use_thread:
            # Put the done callback into the callback queue so that it
            # can be received by a polling function in the main thread
            thread_callback_queue.put([threading.current_thread(), done], block=True)
        else:
            done()

    p = subprocess.Popen(cmd)

    if use_thread:
        threading.Thread(target=wait_for_proc, args=(p,)).start()
    else:
        wait_for_proc(p)

    return p


def compile_shader_pass(res, raw_shaders_path, shader_name, defs, make_variants):
    os.chdir(raw_shaders_path + '/' + shader_name)

    # Open json file
    json_name = shader_name + '.json'
    with open(json_name) as f:
        json_file = f.read()
    json_data = json.loads(json_file)

    fp = arm.utils.get_fp_build()
    arm.lib.make_datas.make(res, shader_name, json_data, fp, defs, make_variants)

    path = fp + '/compiled/Shaders'
    c = json_data['contexts'][0]
    for s in ['vertex_shader', 'fragment_shader', 'geometry_shader', 'tesscontrol_shader', 'tesseval_shader']:
        if s in c:
            shutil.copy(c[s], path + '/' + c[s].split('/')[-1])

def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def export_data(fp, sdk_path):
    wrd = bpy.data.worlds['Arm']

    print('Armory v{0} ({1})'.format(wrd.arm_version, wrd.arm_commit))
    if wrd.arm_verbose_output:
        print(f'Blender: {bpy.app.version_string}, Target: {state.target}, GAPI: {arm.utils.get_gapi()}')

    # Clean compiled variants if cache is disabled
    build_dir = arm.utils.get_fp_build()
    if not wrd.arm_cache_build:
        if os.path.isdir(build_dir + '/debug/html5-resources'):
            shutil.rmtree(build_dir + '/debug/html5-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/krom-resources'):
            shutil.rmtree(build_dir + '/krom-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/debug/krom-resources'):
            shutil.rmtree(build_dir + '/debug/krom-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/windows-resources'):
            shutil.rmtree(build_dir + '/windows-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/linux-resources'):
            shutil.rmtree(build_dir + '/linux-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/osx-resources'):
            shutil.rmtree(build_dir + '/osx-resources', onerror=remove_readonly)
        if os.path.isdir(build_dir + '/compiled/Shaders'):
            shutil.rmtree(build_dir + '/compiled/Shaders', onerror=remove_readonly)

    raw_shaders_path = sdk_path + '/armory/Shaders/'
    assets_path = sdk_path + '/armory/Assets/'
    export_physics = bpy.data.worlds['Arm'].arm_physics != 'Disabled'
    export_navigation = bpy.data.worlds['Arm'].arm_navigation != 'Disabled'
    export_ui = bpy.data.worlds['Arm'].arm_ui != 'Disabled'
    assets.reset()

    # Build node trees
    ArmoryExporter.import_traits = []
    make_logic.build()
    make_world.build()
    make_renderpath.build()

    # Export scene data
    assets.embedded_data = sorted(list(set(assets.embedded_data)))
    physics_found = False
    navigation_found = False
    ui_found = False
    ArmoryExporter.compress_enabled = state.is_publish and wrd.arm_asset_compression
    ArmoryExporter.optimize_enabled = state.is_publish and wrd.arm_optimize_data
    if not os.path.exists(build_dir + '/compiled/Assets'):
        os.makedirs(build_dir + '/compiled/Assets')
    # have a "zoo" collection in the current scene
    export_coll = bpy.data.collections.new("export_coll")
    bpy.context.scene.collection.children.link(export_coll)
    for scene in bpy.data.scenes:
        if scene == bpy.context.scene: continue
        for o in scene.collection.all_objects:
            if o.type == "MESH" or o.type == "EMPTY":
                if o.name not in  export_coll.all_objects.keys():
                    export_coll.objects.link(o)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bpy.data.collections.remove(export_coll) # destroy "zoo" collection

    for scene in bpy.data.scenes:
        if scene.arm_export:
            ext = '.lz4' if ArmoryExporter.compress_enabled else '.arm'
            asset_path = build_dir + '/compiled/Assets/' + arm.utils.safestr(scene.name) + ext
            ArmoryExporter.export_scene(bpy.context, asset_path, scene=scene, depsgraph=depsgraph)
            if ArmoryExporter.export_physics:
                physics_found = True
            if ArmoryExporter.export_navigation:
                navigation_found = True
            if ArmoryExporter.export_ui:
                ui_found = True
            assets.add(asset_path)

    if physics_found == False: # Disable physics if no rigid body is exported
        export_physics = False

    if navigation_found == False:
        export_navigation = False

    if ui_found == False:
        export_ui = False

    if wrd.arm_ui == 'Enabled':
        export_ui = True

    modules = []
    if wrd.arm_audio == 'Enabled':
        modules.append('audio')
    if export_physics:
        modules.append('physics')
    if export_navigation:
        modules.append('navigation')
    if export_ui:
        modules.append('ui')

    defs = arm.utils.def_strings_to_array(wrd.world_defs)
    cdefs = arm.utils.def_strings_to_array(wrd.compo_defs)

    if wrd.arm_verbose_output:
        print('Exported modules:', ', '.join(modules))
        print('Shader flags:', ' '.join(defs))
        print('Compositor flags:', ' '.join(cdefs))
        print('Khafile flags:', ' '.join(assets.khafile_defs))

    # Render path is configurable at runtime
    has_config = wrd.arm_write_config or os.path.exists(arm.utils.get_fp() + '/Bundled/config.arm')

    # Write compiled.inc
    shaders_path = build_dir + '/compiled/Shaders'
    if not os.path.exists(shaders_path):
        os.makedirs(shaders_path)
    write_data.write_compiledglsl(defs + cdefs, make_variants=has_config)

    # Write referenced shader passes
    if not os.path.isfile(build_dir + '/compiled/Shaders/shader_datas.arm') or state.last_world_defs != wrd.world_defs:
        res = {'shader_datas': []}

        for ref in assets.shader_passes:
            # Ensure shader pass source exists
            if not os.path.exists(raw_shaders_path + '/' + ref):
                continue
            assets.shader_passes_assets[ref] = []
            compile_shader_pass(res, raw_shaders_path, ref, defs + cdefs, make_variants=has_config)

        # Workaround to also export non-material world shaders
        res['shader_datas'] += make_world.shader_datas

        arm.utils.write_arm(shaders_path + '/shader_datas.arm', res)

    for ref in assets.shader_passes:
        for s in assets.shader_passes_assets[ref]:
            assets.add_shader(shaders_path + '/' + s + '.glsl')
    for file in assets.shaders_external:
        name = file.split('/')[-1].split('\\')[-1]
        target = build_dir + '/compiled/Shaders/' + name
        if not os.path.exists(target):
            shutil.copy(file, target)
    state.last_world_defs = wrd.world_defs

    # Reset path
    os.chdir(fp)

    # Copy std shaders
    if not os.path.isdir(build_dir + '/compiled/Shaders/std'):
        shutil.copytree(raw_shaders_path + 'std', build_dir + '/compiled/Shaders/std')

    # Write config.arm
    resx, resy = arm.utils.get_render_resolution(arm.utils.get_active_scene())
    if wrd.arm_write_config:
        write_data.write_config(resx, resy)

    # Change project version (Build, Publish)
    if (not state.is_play) and (wrd.arm_project_version_autoinc):
        wrd.arm_project_version = arm.utils.arm.utils.change_version_project(wrd.arm_project_version)

    # Write khafile.js
    write_data.write_khafilejs(state.is_play, export_physics, export_navigation, export_ui, state.is_publish, ArmoryExporter.import_traits)

    # Write Main.hx - depends on write_khafilejs for writing number of assets
    scene_name = arm.utils.get_project_scene_name()
    write_data.write_mainhx(scene_name, resx, resy, state.is_play, state.is_publish)
    if scene_name != state.last_scene or resx != state.last_resx or resy != state.last_resy:
        wrd.arm_recompile = True
        state.last_resx = resx
        state.last_resy = resy
        state.last_scene = scene_name

def compile(assets_only=False):
    wrd = bpy.data.worlds['Arm']
    fp = arm.utils.get_fp()
    os.chdir(fp)

    # Set build command
    target_name = state.target

    node_path = arm.utils.get_node_path()
    khamake_path = arm.utils.get_khamake_path()
    cmd = [node_path, khamake_path]

    kha_target_name = arm.utils.get_kha_target(target_name)
    if kha_target_name != '':
        cmd.append(kha_target_name)

    # Custom exporter
    if state.is_export:
        item = wrd.arm_exporterlist[wrd.arm_exporterlist_index]
        if item.arm_project_target == 'custom' and item.arm_project_khamake != '':
            for s in item.arm_project_khamake.split(' '):
                cmd.append(s)

    ffmpeg_path = arm.utils.get_ffmpeg_path()
    if ffmpeg_path != None and ffmpeg_path != '':
        cmd.append('--ffmpeg')
        cmd.append(ffmpeg_path) # '"' + ffmpeg_path + '"'

    state.export_gapi = arm.utils.get_gapi()
    cmd.append('-g')
    cmd.append(state.export_gapi)
    # Windows - Set Visual Studio Version
    if state.target.startswith('windows'):
        cmd.append('-visualstudio')
        vs_ver, vs_year, vs_name, vs_id = arm.utils.get_visual_studio_from_version(wrd.arm_project_win_list_vs)
        cmd.append(vs_id)

    if arm.utils.get_legacy_shaders() or 'ios' in state.target:
        if 'html5' in state.target or 'ios' in state.target:
            pass
        else:
            cmd.append('--shaderversion')
            cmd.append('110')
    elif 'android' in state.target or 'html5' in state.target:
        cmd.append('--shaderversion')
        cmd.append('300')
    else:
        cmd.append('--shaderversion')
        cmd.append('330')

    if '_VR' in wrd.world_defs:
        cmd.append('--vr')
        cmd.append('webvr')

    if arm.utils.get_pref_or_default('khamake_debug', False):
        cmd.append('--debug')

    if arm.utils.get_rp().rp_renderer == 'Raytracer':
        cmd.append('--raytrace')
        cmd.append('dxr')
        dxc_path = fp + '/HlslShaders/dxc.exe'
        subprocess.Popen([dxc_path, '-Zpr', '-Fo', fp + '/Bundled/raytrace.cso', '-T', 'lib_6_3', fp + '/HlslShaders/raytrace.hlsl']).wait()

    if arm.utils.get_khamake_threads() > 1:
        cmd.append('--parallelAssetConversion')
        cmd.append(str(arm.utils.get_khamake_threads()))

    compilation_server = False

    cmd.append('--to')
    if (kha_target_name == 'krom' and not state.is_publish) or (kha_target_name == 'html5' and not state.is_publish):
        cmd.append(arm.utils.build_dir() + '/debug')
        # Start compilation server
        if kha_target_name == 'krom' and arm.utils.get_compilation_server() and not assets_only and wrd.arm_cache_build:
            compilation_server = True
            arm.lib.server.run_haxe(arm.utils.get_haxe_path())
    else:
        cmd.append(arm.utils.build_dir())

    if not wrd.arm_verbose_output:
        cmd.append("--quiet")
    else:
        print("Using project from " + arm.utils.get_fp())
        print("Running: ", *cmd)

    #Project needs to be compiled at least once
    #before compilation server can work
    if not os.path.exists(arm.utils.build_dir() + '/debug/krom/krom.js') and not state.is_publish:
       state.proc_build = run_proc(cmd, build_done)
    else:
        if assets_only or compilation_server:
            cmd.append('--nohaxe')
            cmd.append('--noproject')
        state.proc_build = run_proc(cmd, assets_done if compilation_server else build_done)

def build(target, is_play=False, is_publish=False, is_export=False):
    global profile_time
    profile_time = time.time()

    state.target = target
    state.is_play = is_play
    state.is_publish = is_publish
    state.is_export = is_export

    # Save blend
    if arm.utils.get_save_on_build():
        bpy.ops.wm.save_mainfile()

    log.clear(clear_warnings=True, clear_errors=True)

    # Set camera in active scene
    active_scene = arm.utils.get_active_scene()
    if active_scene.camera == None:
        for o in active_scene.objects:
            if o.type == 'CAMERA':
                active_scene.camera = o
                break

    # Get paths
    sdk_path = arm.utils.get_sdk_path()
    raw_shaders_path = sdk_path + '/armory/Shaders/'

    # Set dir
    fp = arm.utils.get_fp()
    os.chdir(fp)

    # Create directories
    wrd = bpy.data.worlds['Arm']
    sources_path = 'Sources/' + arm.utils.safestr(wrd.arm_project_package)
    if not os.path.exists(sources_path):
        os.makedirs(sources_path)

    # Save external scripts edited inside Blender
    write_texts = False
    for text in bpy.data.texts:
        if text.filepath != '' and text.is_dirty:
            write_texts = True
            break
    if write_texts:
        area = bpy.context.area
        old_type = area.type
        area.type = 'TEXT_EDITOR'
        for text in bpy.data.texts:
            if text.filepath != '' and text.is_dirty and os.path.isfile(text.filepath):
                area.spaces[0].text = text
                bpy.ops.text.save()
        area.type = old_type

    # Save internal Haxe scripts
    for text in bpy.data.texts:
        if text.filepath == '' and text.name[-3:] == '.hx':
            with open('Sources/' + arm.utils.safestr(wrd.arm_project_package) + '/' + text.name, 'w') as f:
                f.write(text.as_string())

    # Export data
    export_data(fp, sdk_path)

    if state.target == 'html5':
        w, h = arm.utils.get_render_resolution(arm.utils.get_active_scene())
        write_data.write_indexhtml(w, h, is_publish)
        # Bundle files from include dir
        if os.path.isdir('include'):
            dest  = '/html5/' if is_publish else '/debug/html5/'
            for fn in glob.iglob(os.path.join('include', '**'), recursive=False):
                shutil.copy(fn, arm.utils.build_dir() + dest + os.path.basename(fn))

def play_done():
    """Called if the player was stopped/terminated."""
    state.proc_play = None
    state.redraw_ui = True
    log.clear()
    live_patch.stop()

def assets_done():
    if state.proc_build == None:
        return
    result = state.proc_build.poll()
    if result == 0:
        # Connect to the compilation server
        os.chdir(arm.utils.build_dir() + '/debug/')
        cmd = [arm.utils.get_haxe_path(), '--connect', '6000', 'project-krom.hxml']
        state.proc_build = run_proc(cmd, compilation_server_done)
    else:
        state.proc_build = None
        state.redraw_ui = True
        log.error('Build failed, check console')

def compilation_server_done():
    if state.proc_build == None:
        return
    result = state.proc_build.poll()
    if result == 0:
        if os.path.exists('krom/krom.js.temp'):
            os.chmod('krom/krom.js', stat.S_IWRITE)
            os.remove('krom/krom.js')
            os.rename('krom/krom.js.temp', 'krom/krom.js')
        build_done()
    else:
        state.proc_build = None
        state.redraw_ui = True
        log.error('Build failed, check console')

def build_done():
    print('Finished in {:0.3f}s'.format(time.time() - profile_time))
    if log.num_warnings > 0:
        log.print_warn(f'{log.num_warnings} warning{"s" if log.num_warnings > 1 else ""} occurred during compilation')
    if state.proc_build is None:
        return
    result = state.proc_build.poll()
    state.proc_build = None
    state.redraw_ui = True
    if result == 0:
        bpy.data.worlds['Arm'].arm_recompile = False
        build_success()
    else:
        log.error('Build failed, check console')


def runtime_to_target():
    wrd = bpy.data.worlds['Arm']
    if wrd.arm_runtime == 'Krom':
        return 'krom'
    else:
        return 'html5'

def get_khajs_path(target):
    if target == 'krom':
        return arm.utils.build_dir() + '/debug/krom/krom.js'
    else: # Browser
        return arm.utils.build_dir() + '/debug/html5/kha.js'

def play():
    global scripts_mtime
    wrd = bpy.data.worlds['Arm']

    build(target=runtime_to_target(), is_play=True)

    khajs_path = get_khajs_path(state.target)
    if not wrd.arm_cache_build or \
       not os.path.isfile(khajs_path) or \
       assets.khafile_defs_last != assets.khafile_defs or \
       state.last_target != state.target:
        wrd.arm_recompile = True

    state.last_target = state.target

    # Trait sources modified
    state.mod_scripts = []
    script_path = arm.utils.get_fp() + '/Sources/' + arm.utils.safestr(wrd.arm_project_package)
    if os.path.isdir(script_path):
        new_mtime = scripts_mtime
        for fn in glob.iglob(os.path.join(script_path, '**', '*.hx'), recursive=True):
            mtime = os.path.getmtime(fn)
            if scripts_mtime < mtime:
                arm.utils.fetch_script_props(fn) # Trait props
                fn = fn.split('Sources/')[1]
                fn = fn[:-3] #.hx
                fn = fn.replace('/', '.')
                state.mod_scripts.append(fn)
                wrd.arm_recompile = True
                if new_mtime < mtime:
                    new_mtime = mtime
        scripts_mtime = new_mtime
        if len(state.mod_scripts) > 0: # Trait props
            arm.utils.fetch_trait_props()

    compile(assets_only=(not wrd.arm_recompile))

def build_success():
    log.clear()
    wrd = bpy.data.worlds['Arm']

    if state.is_play:
        if wrd.arm_runtime == 'Browser':
            # Start server
            os.chdir(arm.utils.get_fp())
            prefs = arm.utils.get_arm_preferences()
            t = threading.Thread(name='localserver', target=arm.lib.server.run_tcp, args=(prefs.html5_server_port, prefs.html5_server_log), daemon=True)
            t.start()
            html5_app_path = 'http://localhost:{}/{}/debug/html5'.format(prefs.html5_server_port, arm.utils.build_dir())
            webbrowser.open(html5_app_path)
        elif wrd.arm_runtime == 'Krom':
            if wrd.arm_live_patch:
                live_patch.start()
                open(arm.utils.get_fp_build() + '/debug/krom/krom.patch', 'w').close()
            krom_location, krom_path = arm.utils.krom_paths()
            os.chdir(krom_location)
            cmd = [krom_path, arm.utils.get_fp_build() + '/debug/krom', arm.utils.get_fp_build() + '/debug/krom-resources']
            if arm.utils.get_os() == 'win':
                cmd.append('--consolepid')
                cmd.append(str(os.getpid()))
            if wrd.arm_audio == 'Disabled':
                cmd.append('--nosound')
            if wrd.arm_verbose_output:
                print("Running: ", *cmd)
            state.proc_play = run_proc(cmd, play_done)

    elif state.is_publish:
        sdk_path = arm.utils.get_sdk_path()
        target_name = arm.utils.get_kha_target(state.target)
        files_path = os.path.join(arm.utils.get_fp_build(), target_name)

        if (target_name == 'html5' or target_name == 'krom') and wrd.arm_minify_js:
            # Minify JS
            minifier_path = sdk_path + '/lib/armory_tools/uglifyjs/bin/uglifyjs'
            if target_name == 'html5':
                jsfile = files_path + '/kha.js'
            else:
                jsfile = files_path + '/krom.js'
            args = [arm.utils.get_node_path(), minifier_path, jsfile, '-o', jsfile]
            proc = subprocess.Popen(args)
            proc.wait()

        if target_name == 'krom':
            # Copy Krom binaries
            if state.target == 'krom-windows':
                gapi = state.export_gapi
                ext = '' if gapi == 'direct3d11' else '_' + gapi
                krom_location = sdk_path + '/Krom/Krom' + ext + '.exe'
                shutil.copy(krom_location, files_path + '/Krom.exe')
                krom_exe = arm.utils.safestr(wrd.arm_project_name) + '.exe'
                os.rename(files_path + '/Krom.exe', files_path + '/' + krom_exe)
            elif state.target == 'krom-linux':
                krom_location = sdk_path + '/Krom/Krom'
                shutil.copy(krom_location, files_path)
                krom_exe = arm.utils.safestr(wrd.arm_project_name)
                os.rename(files_path + '/Krom', files_path + '/' + krom_exe)
                krom_exe = './' + krom_exe
            else:
                krom_location = sdk_path + '/Krom/Krom.app'
                shutil.copytree(krom_location, files_path + '/Krom.app')
                game_files = os.listdir(files_path)
                for f in game_files:
                    f = files_path + '/' + f
                    if os.path.isfile(f):
                        shutil.move(f, files_path + '/Krom.app/Contents/MacOS')
                krom_exe = arm.utils.safestr(wrd.arm_project_name) + '.app'
                os.rename(files_path + '/Krom.app', files_path + '/' + krom_exe)

            # Rename
            ext = state.target.split('-')[-1] # krom-windows
            new_files_path = files_path + '-' + ext
            os.rename(files_path, new_files_path)
            files_path = new_files_path

        if target_name == 'html5':
            project_path = files_path
            print('Exported HTML5 package to ' + project_path)
        elif target_name.startswith('ios') or target_name.startswith('osx'): # TODO: to macos
            project_path = files_path + '-build'
            print('Exported XCode project to ' + project_path)
        elif target_name.startswith('windows'):
            project_path = files_path + '-build'
            vs_ver, vs_year, vs_name, vs_id = arm.utils.get_visual_studio_from_version(wrd.arm_project_win_list_vs)
            print('Exported '+ vs_name +' project to ' + project_path)
        elif target_name.startswith('android'):
            project_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version)
            project_path = os.path.join(files_path + '-build', project_name)
            print('Exported Android Studio project to ' + project_path)
        elif target_name.startswith('krom'):
            project_path = files_path
            print('Exported Krom package to ' + project_path)
        else:
            project_path = files_path + '-build'
            print('Exported makefiles to ' + project_path)

        if not bpy.app.background and arm.utils.get_arm_preferences().open_build_directory:
            arm.utils.open_folder(project_path)

        # Android build APK
        if target_name.startswith('android'):
            if (arm.utils.get_project_android_build_apk()) and (len(arm.utils.get_android_sdk_root_path()) > 0):
                print("\nBuilding APK")
                # Check settings
                path_sdk = arm.utils.get_android_sdk_root_path()
                if len(path_sdk) > 0:
                    # Check Environment Variables - ANDROID_SDK_ROOT
                    if os.getenv('ANDROID_SDK_ROOT') == None:
                        # Set value from settings
                        os.environ['ANDROID_SDK_ROOT'] = path_sdk
                else:
                    project_path = ''

                # Build start
                if len(project_path) > 0:
                    os.chdir(project_path) # set work folder
                    if arm.utils.get_os_is_windows():
                        state.proc_publish_build = run_proc(os.path.join(project_path, "gradlew.bat assembleDebug"), done_gradlew_build)
                    else:
                        cmd = shlex.split(os.path.join(project_path, "gradlew assembleDebug"))
                        state.proc_publish_build = run_proc(cmd, done_gradlew_build)
                else:
                    print('\nBuilding APK Warning: ANDROID_SDK_ROOT is not specified in environment variables and "Android SDK Path" setting is not specified in preferences: \n- If you specify an environment variable ANDROID_SDK_ROOT, then you need to restart Blender;\n- If you specify the setting "Android SDK Path" in the preferences, then repeat operation "Publish"')

        # HTML5 After Publish
        if target_name.startswith('html5'):
            if len(arm.utils.get_html5_copy_path()) > 0 and (wrd.arm_project_html5_copy):
                project_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version)
                dst = os.path.join(arm.utils.get_html5_copy_path(), project_name)
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                try:
                    shutil.copytree(project_path, dst)
                    print("Copied files to " + dst)
                except OSError as exc:
                    if exc.errno == errno.ENOTDIR:
                        shutil.copy(project_path, dst)
                    else: raise
                if len(arm.utils.get_link_web_server()) and (wrd.arm_project_html5_start_browser):
                    link_html5_app = arm.utils.get_link_web_server() +'/'+ project_name
                    print("Running a browser with a link " + link_html5_app)
                    webbrowser.open(link_html5_app)

        # Windows After Publish
        if target_name.startswith('windows'):
            list_vs = []
            err = ''
            # Print message
            project_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version)
            if int(wrd.arm_project_win_build) == 1:
                print('\nOpen in Visual Studio: ' + os.path.join(project_path, project_name + '.sln"'))
            if int(wrd.arm_project_win_build) == 2:
                print('\nCompile project ' + os.path.join(project_path, project_name + '.vcxproj'))
            if int(wrd.arm_project_win_build) == 3:
                print('\nCompile and run project ' + os.path.join(project_path, project_name + '.vcxproj'))
            if int(wrd.arm_project_win_build) > 0:
                # Check Visual Studio
                list_vs, err = arm.utils.get_list_installed_vs(True, True, True)
                if len(err) > 0:
                    print(err)
                    return
                if len(list_vs) == 0:
                    print('No Visual Studio found')
                    return
                is_check = False
                for vs in list_vs:
                    if vs[0] == wrd.arm_project_win_list_vs:
                        is_check = True
                        break
                if not is_check:
                    vs_ver, vs_year, vs_name, vs_id = arm.utils.get_visual_studio_from_version(wrd.arm_project_win_list_vs)
                    print(vs_name + ' not found.')
                    print('The following are installed on the PC:')
                    for vs in list_vs:
                        print('- ' + vs[1] + ' (version ' + vs[3] +')')
                    return
                # Current VS
                vs_path = ''
                for vs in list_vs:
                    if vs[0] == wrd.arm_project_win_list_vs:
                        vs_path = vs[2]
                        break
                # Open in Visual Studio
                if int(wrd.arm_project_win_build) == 1:
                    cmd = os.path.join('start "' + vs_path, 'Common7', 'IDE', 'devenv.exe" "' + os.path.join(project_path, project_name + '.sln"'))
                    subprocess.Popen(cmd, shell=True)
                # Compile
                if int(wrd.arm_project_win_build) > 1:
                    bits = '64' if wrd.arm_project_win_build_arch == 'x64' else '32'
                    # vcvars
                    cmd = os.path.join(vs_path, 'VC', 'Auxiliary', 'Build', 'vcvars' + bits + '.bat')
                    if not os.path.isfile(cmd):
                        print('File "'+ cmd +'" not found. Verify ' + vs_name + ' was installed correctly')
                        log.error('Compile failed, check console')
                        return
                    state.proc_publish_build = run_proc(cmd, done_vs_vars)

def done_gradlew_build():
    if state.proc_publish_build == None:
        return
    result = state.proc_publish_build.poll()
    if result == 0:
        state.proc_publish_build = None

        wrd = bpy.data.worlds['Arm']
        path_apk = os.path.join(arm.utils.get_fp_build(), arm.utils.get_kha_target(state.target))
        project_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version)
        path_apk = os.path.join(path_apk + '-build', project_name, 'app', 'build', 'outputs', 'apk', 'debug')

        print("\nBuild APK to " + path_apk)
        # Rename APK
        apk_name = 'app-debug.apk'
        file_name = os.path.join(path_apk, apk_name)
        if wrd.arm_project_android_rename_apk:
            apk_name = project_name + '.apk'
            os.rename(file_name, os.path.join(path_apk, apk_name))
            file_name = os.path.join(path_apk, apk_name)
            print("\nRename APK to " + apk_name)
        # Copy APK
        if wrd.arm_project_android_copy_apk:
            shutil.copyfile(file_name, os.path.join(arm.utils.get_android_apk_copy_path(), apk_name))
            print("Copy APK to " + arm.utils.get_android_apk_copy_path())
        # Open directory with APK
        if arm.utils.get_android_open_build_apk_directory():
            arm.utils.open_folder(path_apk)
        # Open directory after copy APK
        if arm.utils.get_android_apk_copy_open_directory():
            arm.utils.open_folder(arm.utils.get_android_apk_copy_path())
        # Running emulator
        if wrd.arm_project_android_run_avd:
            run_android_emulators(arm.utils.get_android_emulator_name())
        state.redraw_ui = True
    else:
        state.proc_publish_build = None
        state.redraw_ui = True
        os.environ['ANDROID_SDK_ROOT'] = ''
        log.error('Building the APK failed, check console')

def run_android_emulators(avd_name):
    if len(avd_name.strip()) == 0:
        return
    print('\nRunning Emulator "'+ avd_name +'"')
    path_file = arm.utils.get_android_emulator_file()
    if len(path_file) > 0:
        if arm.utils.get_os_is_windows():
            run_proc(path_file + " -avd "+ avd_name, None)
        else:
            cmd = shlex.split(path_file + " -avd "+ avd_name)
            run_proc(cmd, None)
    else:
        print('Update List Emulators Warning: File "'+ path_file +'" not found. Check that the variable ANDROID_SDK_ROOT is correct in environment variables or in "Android SDK Path" setting: \n- If you specify an environment variable ANDROID_SDK_ROOT, then you need to restart Blender;\n- If you specify the setting "Android SDK Path", then repeat operation "Publish"')

def done_vs_vars():
    if state.proc_publish_build == None:
        return
    result = state.proc_publish_build.poll()
    if result == 0:
        state.proc_publish_build = None
        # MSBuild
        wrd = bpy.data.worlds['Arm']
        list_vs, err = arm.utils.get_list_installed_vs(True, True, True)
        # Current VS
        vs_path = ''
        vs_name = ''
        for vs in list_vs:
            if vs[0] == wrd.arm_project_win_list_vs:
                vs_name = vs[1]
                vs_path = vs[2]
                break
        msbuild = os.path.join(vs_path, 'MSBuild', 'Current', 'Bin', 'MSBuild.exe')
        if not os.path.isfile(msbuild):
            print('File "'+ msbuild +'" not found. Verify ' + vs_name + ' was installed correctly')
            log.error('Compile failed, check console')
            state.redraw_ui = True
            return
        project_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version)
        project_path = os.path.join(arm.utils.get_fp_build(), arm.utils.get_kha_target(state.target)) + '-build'
        cmd = '"' + msbuild + '" "' + os.path.join(project_path, project_name + '.vcxproj"')
        # Arguments
        platform = 'x64' if wrd.arm_project_win_build_arch == 'x64' else 'win32'
        log_param = wrd.arm_project_win_build_log
        if log_param == 'WarningsAndErrorsOnly':
            log_param = 'WarningsOnly;ErrorsOnly'
        cmd = cmd + ' -m:' + str(wrd.arm_project_win_build_cpu) + ' -clp:'+ log_param +' /p:Configuration='+ wrd.arm_project_win_build_mode +' /p:Platform=' + platform
        print('\nCompiling the project ' + os.path.join(project_path, project_name + '.vcxproj"'))
        state.proc_publish_build = run_proc(cmd, done_vs_build)
        state.redraw_ui = True
    else:
        state.proc_publish_build = None
        state.redraw_ui = True
        log.error('\nCompile failed, check console')

def done_vs_build():
    if state.proc_publish_build == None:
        return
    result = state.proc_publish_build.poll()
    if result == 0:
        state.proc_publish_build = None

        wrd = bpy.data.worlds['Arm']
        project_path = os.path.join(arm.utils.get_fp_build(), arm.utils.get_kha_target(state.target)) + '-build'
        if wrd.arm_project_win_build_arch == 'x64':
            path = os.path.join(project_path, 'x64', wrd.arm_project_win_build_mode)
        else:
            path = os.path.join(project_path, wrd.arm_project_win_build_mode)
        print('\nCompilation completed in ' + path)
        # Run
        if int(wrd.arm_project_win_build) == 3:
            # Copying the executable file
            res_path = os.path.join(arm.utils.get_fp_build(), arm.utils.get_kha_target(state.target))
            file_name = arm.utils.safesrc(wrd.arm_project_name +'-'+ wrd.arm_project_version) + '.exe'
            print('\nCopy the executable file from ' + path + ' to ' + res_path)
            shutil.copyfile(os.path.join(path, file_name), os.path.join(res_path, file_name))
            path = res_path
            # Run project
            cmd = os.path.join('"' + res_path, file_name + '"')
            print('Run the executable file to ' + cmd)
            os.chdir(res_path) # set work folder
            subprocess.Popen(cmd, shell=True)
        # Open Build Directory
        if wrd.arm_project_win_build_open:
            arm.utils.open_folder(path)
        state.redraw_ui = True
    else:
        state.proc_publish_build = None
        state.redraw_ui = True
        log.error('Compile failed, check console')

def clean():
    os.chdir(arm.utils.get_fp())
    wrd = bpy.data.worlds['Arm']

    # Remove build and compiled data
    try:
        if os.path.isdir(arm.utils.build_dir()):
            shutil.rmtree(arm.utils.build_dir(), onerror=remove_readonly)
        if os.path.isdir(arm.utils.get_fp() + '/build'): # Kode Studio build dir
            shutil.rmtree(arm.utils.get_fp() + '/build', onerror=remove_readonly)
    except:
        print('Armory Warning: Some files in the build folder are locked')

    # Remove compiled nodes
    pkg_dir = arm.utils.safestr(wrd.arm_project_package).replace('.', '/')
    nodes_path = 'Sources/' + pkg_dir + '/node/'
    if os.path.isdir(nodes_path):
        shutil.rmtree(nodes_path, onerror=remove_readonly)

    # Remove khafile/Main.hx
    if os.path.isfile('khafile.js'):
        os.remove('khafile.js')
    if os.path.isfile('Sources/Main.hx'):
        os.remove('Sources/Main.hx')

    # Remove Sources/ dir if empty
    if os.path.exists('Sources/' + pkg_dir) and os.listdir('Sources/' + pkg_dir) == []:
        shutil.rmtree('Sources/' + pkg_dir, onerror=remove_readonly)
        if os.path.exists('Sources') and os.listdir('Sources') == []:
            shutil.rmtree('Sources/', onerror=remove_readonly)

    # Remove Shape key Textures
    if os.path.exists('MorphTargets/'):
        shutil.rmtree('MorphTargets/', onerror=remove_readonly)

    # To recache signatures for batched materials
    for mat in bpy.data.materials:
        mat.signature = ''
        mat.arm_cached = False

    # Restart compilation server
    if arm.utils.get_compilation_server():
        arm.lib.server.kill_haxe()

    print('Project cleaned')
