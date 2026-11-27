"""Original batch layout, with watch-first/modem-second workflow and live versions."""
import datetime
import json
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .runner import BatchRunner
from .settings import Settings, version_from_filename, modem_versions_from_filename
from .workflow import Workflow


class BatchWindow:
    def __init__(self, parent=None, firmware=''):
        self.window = tk.Tk() if parent is None else tk.Toplevel(parent)
        if parent is not None:
            self.window.transient(parent)
            self.window.grab_set()
        self.window.title('批量 OTA V%s' % __version__)
        self.window.geometry('980x950')
        self.window.minsize(900, 760)
        self.running = False
        self._close_when_done = False
        self.runner = None
        self.queue = queue.Queue()
        self.rows = {}
        self.information = {}
        self.log_records = []
        self.probe_records = []
        self._result_tip = None
        self._result_tip_after = None
        self._result_tip_row = None
        self._result_tip_text = tk.StringVar()
        self.prefix = tk.StringVar(value='XFN_')
        self.rssi = tk.StringVar(value='-80')
        self.max_attempts = tk.StringVar(value='3')
        self.concurrency = tk.StringVar(value='3')
        self.firmware = tk.StringVar()
        self.local_version = tk.StringVar()
        self.reconnect_timeout = tk.StringVar(value='120')
        self.firmware.trace_add('write', self._file_version)
        self.skip_same_version = tk.BooleanVar(value=True)
        self.alert_on_success = tk.BooleanVar(value=True)
        self.shine = tk.BooleanVar(value=False)
        self.modem = tk.BooleanVar(value=False)
        self.modem_file = tk.StringVar()
        self.modem_file.trace_add('write', self._modem_file_changed)
        self.modem_remote = tk.StringVar(value='/NOR:/modem_update.bin')
        self.overwrite = tk.BooleanVar(value=True)
        self.modem_source = tk.StringVar()
        self.modem_target = tk.StringVar()
        self._auto_modem_versions = None
        self.send_timeout = tk.StringVar(value='20')
        self.wait_timeout = tk.StringVar(value='20')
        self.status = tk.StringVar(value='未开始')
        form = ttk.LabelFrame(self.window, text='筛选与升级内容')
        form.pack(fill='x', padx=10, pady=8)
        form.columnconfigure(1, weight=1)
        self.form, self.controls = form, []
        self.entries = {}
        fields = ((1, '蓝牙名筛选', self.prefix), (2, '最低 RSSI (dBm)', self.rssi),
                  (4, '手表 OTA 文件', self.firmware),
                  (5, '固件版本号', self.local_version), (6, '回连超时（秒）', self.reconnect_timeout),
                  (10, 'modem 本地文件', self.modem_file), (11, '手表目标文件路径', self.modem_remote))
        for row, title, var in fields:
            ttk.Label(form, text=title).grid(row=row, column=0, sticky='w', padx=6, pady=3)
            entry = ttk.Entry(form, textvariable=var)
            entry.grid(row=row, column=1, sticky='ew', padx=6)
            self.controls.append(entry)
            self.entries[title] = entry
        self._paired_entries(3, '批量执行', (
            ('最大尝试次数', self.max_attempts, '最大尝试次数'),
            ('同时台数', self.concurrency, '同时台数')))
        for row, callback in ((4, self._browse), (10, self._browse_modem)):
            button = ttk.Button(form, text='浏览', command=callback)
            button.grid(row=row, column=2, padx=6)
            self.controls.append(button)
        for row, label, var in (
                (7, '同版本号跳过', self.skip_same_version),
                (8, 'ShineFit UUID（E852/E853/E854）', self.shine),
                (9, '升级 modem（可单独执行；选择 OTA 时在手表升级后执行）', self.modem),
                (12, '允许覆盖目标同名文件', self.overwrite)):
            button = ttk.Checkbutton(form, text=label, variable=var)
            button.grid(row=row, column=1, sticky='w', padx=6, pady=3)
            self.controls.append(button)
        self._paired_entries(13, 'modem 版本', (
            ('当前版本', self.modem_source, 'modem 当前版本'),
            ('目标版本', self.modem_target, 'modem 目标版本')))
        self._paired_entries(14, 'modem 超时（秒）', (
            ('发送超时', self.send_timeout, '发送超时（秒）'),
            ('升级超时', self.wait_timeout, '升级超时（秒）')))
        alert_check = ttk.Checkbutton(form, text='升级完成亮屏和长震', variable=self.alert_on_success)
        alert_check.grid(row=15, column=1, sticky='w', padx=6, pady=3)
        self.controls.append(alert_check)
        help_frame = ttk.Frame(form)
        help_frame.grid(row=16, column=0, columnspan=3, sticky='w', padx=6, pady=5)
        for line in (
                '蓝牙名：无逗号按前缀；英文逗号分隔时仅匹配这些完整名称。',
                'OTA 文件留空则不升级手表；仅选 modem 文件可单独升级 modem。',
                'modem 版本可填完整版本或3字符；v030_v031_delta.par 自动填入当前030、目标031。',
                '同时台数默认3台，可设1～20台；停止后等待所有正在处理的设备完成。',
                '传包时也会短时扫描新设备并排队；有空位后连接，回连核验优先。',
                '发送与升级超时均按对应进度连续不变计时，进度变化就重新计时。',
                '超时先取消，退出后再重试；无法取消或结果未知时转下一台。'):
            ttk.Label(help_frame, text=line).pack(anchor='w', pady=(0, 3))
        buttons = ttk.Frame(self.window)
        buttons.pack(fill='x', padx=10)
        self.start_button = ttk.Button(buttons, text='开始批量执行', command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(buttons, text='导出记录', command=self._export).pack(side='right')
        self.stop_button = ttk.Button(buttons, text='停止（完成正在处理的设备）', command=self.request_stop,
                                      state='disabled')
        self.stop_button.pack(side='left', padx=8)
        ttk.Label(self.window, textvariable=self.status).pack(fill='x', padx=10, pady=6)
        self.tree = ttk.Treeview(self.window, columns=('rssi', 'address', 'name', 'firmware', 'modem_version', 'status'),
                                 show='headings', height=9)
        for name, title, width in (('rssi', 'RSSI', 85), ('address', '地址', 125), ('name', '名称', 155),
                                    ('firmware', '固件版本', 170), ('modem_version', 'modem 版本', 210),
                                    ('status', '结果', 280)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=(name == 'status'),
                             anchor='w' if name == 'status' else 'center')
        style = ttk.Style(self.window)
        self._version_font = tkfont.Font(root=self.window,
            font=style.lookup('Treeview', 'font') or 'TkDefaultFont')
        self._heading_font = tkfont.Font(root=self.window,
            font=style.lookup('Treeview.Heading', 'font') or 'TkHeadingFont')
        self._fit_version_columns()
        self.tree.pack(fill='both', expand=True, padx=10)
        self.tree.bind('<Motion>', self._result_hover)
        self.tree.bind('<Leave>', lambda event: self._hide_result_tip())
        self.tree.bind('<ButtonPress>', lambda event: self._hide_result_tip())
        self.log = tk.Text(self.window, height=9, state='disabled')
        self.log.pack(fill='both', expand=True, padx=10, pady=8)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.after = self.window.after(100, self._poll)
        if firmware:
            self.firmware.set(firmware)

    def _paired_entries(self, row, title, fields):
        ttk.Label(self.form, text=title).grid(row=row, column=0, sticky='w', padx=6, pady=3)
        group = ttk.Frame(self.form)
        group.grid(row=row, column=1, sticky='ew', padx=6)
        for index, (label, var, key) in enumerate(fields):
            group.columnconfigure(index * 2 + 1, weight=1)
            ttk.Label(group, text=label).grid(row=0, column=index * 2, sticky='w', padx=(0, 6))
            entry = ttk.Entry(group, textvariable=var, width=18)
            entry.grid(row=0, column=index * 2 + 1, sticky='ew',
                       padx=(0, 12 if index < len(fields) - 1 else 0))
            self.entries[key] = entry
            self.controls.append(entry)

    def _file_version(self, *args):
        self.local_version.set(version_from_filename(self.firmware.get()))

    def _modem_file_changed(self, *args):
        path = self.modem_file.get().strip()
        self.modem.set(bool(path))
        versions = modem_versions_from_filename(path)
        fields = (self.modem_source, self.modem_target)
        if versions:
            for field, value in zip(fields, versions):
                field.set(value)
        elif self._auto_modem_versions:
            # Do not retain another package's inferred revisions. Preserve
            # fields that the operator deliberately edited themselves.
            for field, previous in zip(fields, self._auto_modem_versions):
                if field.get() == previous:
                    field.set('')
        self._auto_modem_versions = versions

    def _browse(self):
        path = filedialog.askopenfilename(parent=self.window, title='选择手表 OTA 文件')
        if path:
            self.firmware.set(path)

    def _browse_modem(self):
        path = filedialog.askopenfilename(parent=self.window, title='选择 modem 补丁包')
        if path:
            self.modem_file.set(path)

    def _event(self, kind, data):
        self.queue.put((kind, data))

    def _fit_version_columns(self):
        for column in ('firmware', 'modem_version'):
            if column == 'modem_version':
                width = self._version_font.measure('00000_0000_0000.000.0000.000')
            else:
                width = self._heading_font.measure(self.tree.heading(column, 'text'))
                for row in self.rows.values():
                    width = max(width, self._version_font.measure(self.tree.set(row, column)))
                width += 2 * self._version_font.measure('00')
                self.tree.column(column, anchor='center')
            self.tree.column(column, width=width + 16, minwidth=width + 16, stretch=False)

    def _result_hover(self, event):
        row = self.tree.identify_row(event.y)
        if self.tree.identify_column(event.x) != '#6' or not row:
            self._hide_result_tip()
            return
        if row != self._result_tip_row:
            self._hide_result_tip()
            self._result_tip_row = row
            self._result_tip_after = self.window.after(400, lambda: self._show_result_tip(row))

    def _show_result_tip(self, row):
        self._result_tip_after = None
        if row != self._result_tip_row or not self.tree.exists(row):
            return
        self._result_tip_text.set(self.tree.set(row, 'status'))
        tip = self._result_tip = tk.Toplevel(self.window)
        tip.withdraw()
        tip.overrideredirect(True)
        tk.Label(tip, textvariable=self._result_tip_text, justify='left', wraplength=600,
                 background='#ffffe0', foreground='#202020', relief='solid', borderwidth=1,
                 padx=8, pady=6).pack()
        tip.update_idletasks()
        x = max(0, min(self.window.winfo_pointerx() + 12,
                       self.window.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        y = max(0, min(self.window.winfo_pointery() + 20,
                       self.window.winfo_screenheight() - tip.winfo_reqheight() - 8))
        tip.geometry('+%d+%d' % (x, y))
        tip.deiconify()

    def _hide_result_tip(self):
        if self._result_tip_after:
            self.window.after_cancel(self._result_tip_after)
            self._result_tip_after = None
        if self._result_tip:
            self._result_tip.destroy()
            self._result_tip = None
        self._result_tip_row = None

    def settings(self):
        return Settings(prefix=self.prefix.get().strip(), min_rssi=int(self.rssi.get()),
            max_attempts=int(self.max_attempts.get()), concurrency=int(self.concurrency.get()),
            firmware=self.firmware.get().strip(),
            local_version=self.local_version.get().strip(),
            reconnect_timeout=float(self.reconnect_timeout.get()) if self.firmware.get().strip() else 120,
            skip_same=self.skip_same_version.get(), alert_on_success=self.alert_on_success.get(),
            shine=self.shine.get(), modem=self.modem.get(),
            modem_file=self.modem_file.get().strip(), remote=self.modem_remote.get().strip(),
            overwrite=self.overwrite.get(), modem_source=self.modem_source.get().strip(),
            modem_target=self.modem_target.get().strip(), send_timeout=float(self.send_timeout.get()),
            wait_timeout=float(self.wait_timeout.get())).validate()

    def start(self):
        if self.running:
            return
        try:
            settings = self.settings()
            workflow = Workflow(settings, on_version=lambda address, kind, value:
                                self._event('version', (address, kind, value)),
                on_information=lambda address, label, value:
                    self._event('information', (address, label, value)))
            self.runner = BatchRunner(settings, workflow, self._event)
        except Exception as error:
            messagebox.showerror('参数错误', str(error), parent=self.window)
            return
        self._hide_result_tip()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.information.clear()
        self.log_records.clear()
        self.probe_records.clear()
        self._fit_version_columns()
        self.running = True
        for widget in self.controls:
            widget.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        rule = '仅允许 %s' % settings.prefix if ',' in settings.prefix else '名称以 %s 开头' % settings.prefix
        self.status.set('持续扫描：%s，RSSI ≥ %d；同时%d台；每台最多%d次' %
                        (rule, settings.min_rssi, settings.concurrency, settings.max_attempts))
        tasks = []
        if settings.watch_enabled:
            tasks.append('手表目标版本 %s' % settings.target_watch_version)
        if settings.modem:
            tasks.append('modem %s → %s' % (settings.modem_source, settings.modem_target))
        self._event('log', '开始批量任务：' + '；'.join(tasks))
        threading.Thread(target=self.runner.run, name='batch-ota', daemon=True).start()

    def request_stop(self):
        if self.runner:
            self.runner.stop.set()
        if self.running:
            self.status.set('已停止接收新设备；等待所有正在处理的设备完成，不强制中断刷写')

    def _poll(self):
        versions_changed = False
        try:
            for _ in range(200):
                kind, data = self.queue.get_nowait()
                if kind == 'device':
                    raw_address, name, rssi, status = data
                    address = raw_address.upper()
                    signal = str(rssi) if isinstance(rssi, (int, float)) and -127 <= rssi <= 0 else ''
                    if address not in self.rows:
                        self.rows[address] = self.tree.insert('', 'end',
                            values=(signal, raw_address, name, '', '', status))
                    else:
                        if signal:
                            self.tree.set(self.rows[address], 'rssi', signal)
                        self.tree.set(self.rows[address], 'name', name)
                        self.tree.set(self.rows[address], 'status', status)
                    if self.rows[address] == self._result_tip_row:
                        self._result_tip_text.set(status)
                elif kind == 'version':
                    raw_address, version_kind, value = data
                    column = {2: 'firmware', 5: 'modem_version'}.get(version_kind)
                    if column:
                        address = raw_address.upper()
                        if address not in self.rows:
                            self.rows[address] = self.tree.insert('', 'end',
                                values=('', raw_address, '', '', '', ''))
                        label = '手表固件版本来源' if version_kind == 2 else 'modem 版本来源'
                        source = self.information.get(address, {}).get(label, '')
                        shown_source = 'DIS' if source.startswith('DIS') else source
                        self.tree.set(self.rows[address], column,
                                      value + (' [%s]' % shown_source if shown_source else ''))
                        versions_changed = True
                elif kind == 'diagnostic':
                    self.probe_records.append(data)
                elif kind == 'information':
                    address, label, value = data
                    details = self.information.setdefault(address.upper(), {})
                    details[label] = value
                elif kind == 'log':
                    self.log.configure(state='normal')
                    stamp = datetime.datetime.now().strftime('%H:%M:%S')
                    self.log_records.append('[%s] %s' % (stamp, data))
                    self.log.insert('end', '[%s] %s\n' % (stamp, data))
                    if int(self.log.index('end-1c').split('.')[0]) > 3000:
                        self.log.delete('1.0', '501.0')
                    self.log.see('end')
                    self.log.configure(state='disabled')
                elif kind == 'done':
                    self.running = False
                    if self._close_when_done:
                        self.close()
                        return
                    for widget in self.controls:
                        widget.configure(state='normal')
                    self.start_button.configure(state='normal')
                    self.stop_button.configure(state='disabled')
                    self.status.set('已停止，本轮处理 %d 台；重新开始将建立新一轮记录' % len(self.rows))
        except queue.Empty:
            pass
        if versions_changed:
            self._fit_version_columns()
        self.after = self.window.after(100, self._poll)

    def _export(self):
        path = filedialog.asksaveasfilename(parent=self.window, title='导出升级记录',
            defaultextension='.json', filetypes=[('JSON 记录', '*.json')])
        if path:
            try:
                record = dict(devices=self.information,
                    results={address: self.tree.item(row)['values'] for address, row in self.rows.items()},
                    log='\n'.join(self.log_records), verification_diagnostics=self.probe_records)
                with open(path, 'w', encoding='utf-8') as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
            except OSError as error:
                messagebox.showerror('导出失败', str(error), parent=self.window)

    def close(self):
        if self.running:
            self._close_when_done = True
            self.request_stop()
            return
        self.window.after_cancel(self.after)
        self._hide_result_tip()
        self.window.destroy()


def main():
    ui = BatchWindow()
    ui.window.mainloop()
    return 0
