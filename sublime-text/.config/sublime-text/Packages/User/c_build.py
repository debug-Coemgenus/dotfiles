"""
PLUGIN FOR BULDING AND RUNNING CURRENTLY OPEN C FILE
"""

import sublime
import sublime_plugin
import subprocess
import threading
import os
import configparser
import shlex
import re
import json


class CBuildCommand(sublime_plugin.WindowCommand):
    encoding = 'utf-8'
    killed = False
    proc = None
    panel = None
    panel_lock = threading.Lock()
    ARGS_FILE = '/tmp/c_build_args.json'

    def is_enabled(self, debug=False, run_target=True, kill=False):
        if kill:
            return self.proc is not None and self.proc.poll() is None
        return True

    def run(self, debug=False, run_target=True, kill=False):
        if kill:
            if self.proc:
                self.killed = True
                self.proc.terminate()
            return

        vars = self.window.extract_variables()
        if 'file_path' not in vars:
            sublime.error_message('No file open')
            return

        self._debug = debug
        self._run_target = run_target
        self._working_dir = vars.get('folder', vars['file_path'])
        self._file_base_name = vars['file_base_name']
        self._file_full_path = vars['file']

        if not run_target or debug:
            self._program_args = []
            self.do_build()
            return

        view = self.window.active_view()
        if view and view.file_name() == self._file_full_path and not self._main_takes_args(view):
            self._program_args = []
            self.do_build()
            return

        cached_args = self._load_args(self._file_full_path)

        self.window.show_input_panel(
            'Program arguments:',
            cached_args,
            self.on_args_submitted,
            None,
            None
        )

    def on_args_submitted(self, text):
        self._save_args(self._file_full_path, text)

        try:
            self._program_args = shlex.split(text) if text.strip() else []
        except ValueError as e:
            sublime.error_message(f'Invalid arguments: {e}')
            return

        self.do_build()

    def _main_takes_args(self, view):
        content = view.substr(sublime.Region(0, view.size()))
        match = re.search(r'\bmain\s*\(([\s\S]*?)\)', content)
        if not match:
            return False
        params = match.group(1)
        return 'argc' in params and 'argv' in params

    def _load_args(self, file_path):
        try:
            with open(self.ARGS_FILE, 'r') as f:
                data = json.load(f)
                return data.get(file_path, '')
        except (FileNotFoundError, json.JSONDecodeError):
            return ''

    def _save_args(self, file_path, args):
        data = {}
        try:
            with open(self.ARGS_FILE, 'r') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        data[file_path] = args

        with open(self.ARGS_FILE, 'w') as f:
            json.dump(data, f)
        try:
            os.chmod(self.ARGS_FILE, 0o600)
        except OSError:
            pass

    def do_build(self):
        working_dir = self._working_dir
        file_full_path = self._file_full_path
        file_base_name = self._file_base_name
        debug = self._debug

        config_path = os.path.join(working_dir, 'build.ini')
        config = configparser.ConfigParser()

        if os.path.exists(config_path):
            config.read(config_path)

        std = config.get('compiler', 'std', fallback='c11')
        pedantic = config.getboolean('compiler', 'pedantic', fallback=False)
        warnings = config.get('compiler', 'warnings', fallback='-Wall -Wextra')

        bin_dir = os.path.join(working_dir, 'bin')
        if not os.path.exists(bin_dir):
            os.makedirs(bin_dir)

        output_path = os.path.join(bin_dir, file_base_name)

        args = ['gcc', f'-std={std}']

        if pedantic:
            args.append('-pedantic')

        if warnings:
            args.extend(warnings.split())

        if debug:
            args.append('-g')

        args.extend([file_full_path, '-o', output_path])

        with self.panel_lock:
            self.panel = self.window.create_output_panel('exec')
            settings = self.panel.settings()
            settings.set('result_file_regex', r'^(.*):(\d+):(\d+):\s+(.*)$')
            settings.set('result_base_dir', working_dir)
            self.window.run_command('show_panel', {'panel': 'output.exec'})

        if self.proc is not None:
            self.proc.terminate()
            self.proc = None

        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=working_dir
        )
        self.killed = False

        threading.Thread(
            target=self.read_handle,
            args=(self.proc.stdout, output_path, working_dir)
        ).start()

    def read_handle(self, handle, output_path, working_dir):
        chunk_size = 2 ** 13
        out = b''

        while True:
            try:
                data = os.read(handle.fileno(), chunk_size)
                out += data
                if len(data) == chunk_size:
                    continue
                if data == b'' and out == b'':
                    raise IOError('EOF')
                self.queue_write(out.decode(self.encoding))
                if data == b'':
                    raise IOError('EOF')
                out = b''
            except (UnicodeDecodeError) as e:
                msg = 'Error decoding output using %s - %s'
                self.queue_write(msg % (self.encoding, str(e)))
                break
            except (IOError):
                if self.killed:
                    msg = '\n[Cancelled]'
                else:
                    self.proc.wait()
                    if self.proc.returncode == 0:
                        msg = '\n[Build successful]'
                        if self._run_target:
                            if self._debug:
                                self.launch_gf2(output_path, working_dir)
                            else:
                                self.launch_kitty(output_path, working_dir)
                    else:
                        msg = f'\n[Build failed with code {self.proc.returncode}]'
                self.queue_write(msg)
                break

    def launch_kitty(self, output_path, working_dir):
        program_args = self._program_args

        def run():
            try:
                output_quoted = shlex.quote(output_path)
                args_quoted = ' '.join(shlex.quote(a) for a in program_args)
                if args_quoted:
                    shell_cmd = f'{output_quoted} {args_quoted}; echo; echo "Press Enter to exit"; read'
                else:
                    shell_cmd = f'{output_quoted}; echo; echo "Press Enter to exit"; read'
                cmd = ['kitty', '--directory', working_dir, 'sh', '-c', shell_cmd]
                subprocess.Popen(cmd, cwd=working_dir)
            except Exception as e:
                self.queue_write(f'\n[Error launching kitty: {e}]')
        sublime.set_timeout(run, 100)

    def launch_gf2(self, output_path, working_dir):
        def run():
            try:
                subprocess.Popen(['gf2'], cwd=working_dir)
            except Exception as e:
                self.queue_write(f'\n[Error launching gf2: {e}]')
        sublime.set_timeout(run, 100)

    def queue_write(self, text):
        sublime.set_timeout(lambda: self.do_write(text), 1)

    def do_write(self, text):
        with self.panel_lock:
            self.panel.run_command('append', {'characters': text})
