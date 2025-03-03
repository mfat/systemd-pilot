#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '4')
from gi.repository import Gtk, GLib, Gio, Gdk, Pango, GtkSource
import paramiko
import keyring
import json
import threading
import logging
from pathlib import Path
from typing import Dict, List, Optional
import asyncio
from dataclasses import dataclass
import subprocess
import socket
from rich.console import Console
from rich.text import Text
from io import StringIO
import os
import re

# Add at the top of the file, after imports
APP_NAME = "systemd Pilot"
APP_VERSION = "3.0.0"
APP_DESCRIPTION = """
A graphical tool for managing systemd services locally and remotely.

"""
APP_AUTHORS = ["mFat"]
APP_WEBSITE = "https://github.com/mfat/systemd-pilot"
APP_LICENSE = "GNU General Public License v3.0"
APP_ID = "io.github.mfat.systemdpilot"

@dataclass
class RemoteHost:
    name: str
    hostname: str
    username: str
    auth_type: str  # 'password' or 'key'
    key_path: Optional[str] = None

def get_system_theme_preference():
    """Detect system theme preference"""
    # Try gsettings first
    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        gtk_theme = settings.get_string("gtk-theme")
        return "dark" in gtk_theme.lower()
    except:
        pass
    
    # Fallback to environment variable
    gtk_theme = os.getenv("GTK_THEME", "")
    return "dark" in gtk_theme.lower()

class SystemdManagerWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"{APP_NAME} v{APP_VERSION}")
        self.set_default_size(1000, 600)
        
        # Initialize theme settings
        self.settings = Gtk.Settings.get_default()
        self.is_dark_mode = get_system_theme_preference()
        self.settings.set_property("gtk-application-prefer-dark-theme", self.is_dark_mode)
        
        # Initialize CSS provider
        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        # Apply initial theme
        self.update_custom_theme()
        
        # Initialize logging with more detail
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Starting Systemd Service Manager")
        
        # Store remote connections
        self.remote_hosts: Dict[str, RemoteHost] = {}
        self.active_connections: Dict[str, paramiko.SSHClient] = {}
        
        # Add near the start of __init__ with other initializations
        self.show_inactive = False  # Track whether to show inactive services
        
        self.setup_ui()
        self.load_saved_hosts()

    def setup_ui(self):
        # Apply initial CSS theme
        self.update_custom_theme()
        
        # Create main layout
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(self.main_box)
        
        # Create stack and switcher
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(300)
        
        stack_switcher = Gtk.StackSwitcher()
        stack_switcher.set_stack(self.stack)
        
        # Create header with stack switcher
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header_box.set_halign(Gtk.Align.CENTER)
        header_box.set_margin_top(12)
        header_box.set_margin_bottom(12)
        header_box.pack_start(stack_switcher, True, True, 0)
        
        # Add pages to stack (only do this once)
        self.stack.add_titled(self.create_local_page(), "local", "Local")
        self.stack.add_titled(self.create_remote_page(), "remote", "Remote")
        
        # Create statusbar
        self.statusbar = Gtk.Statusbar()
        self.statusbar_context = self.statusbar.get_context_id("system_info")
        
        # Add components to main layout
        self.main_box.pack_start(header_box, False, False, 0)
        self.main_box.pack_start(self.stack, True, True, 0)
        self.main_box.pack_start(self.statusbar, False, False, 0)
        
        # Connect stack change signal to update statusbar
        self.stack.connect("notify::visible-child", self.update_statusbar)
        
        # Initial statusbar update
        self.update_statusbar()
        
        # Remove these duplicate lines:
        # self.stack.add_titled(self.create_local_page(), "local", "Local")
        # self.stack.add_titled(self.create_remote_page(), "remote", "Remote")
        
        # Remove this line or change to self.main_box:
        # main_box.pack_start(self.stack, True, True, 0)

    def create_remote_page(self):
        remote_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Left sidebar for host management
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_size_request(250, -1)
        
        # Host management buttons with better spacing and icons
        host_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        host_buttons.set_margin_start(6)
        host_buttons.set_margin_end(6)
        host_buttons.set_margin_top(6)
        host_buttons.set_margin_bottom(12)  # Add more bottom margin
        
        # Create buttons with icons
        add_host_btn = Gtk.Button(label="Add Host")
        add_host_btn.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        add_host_btn.set_always_show_image(True)
        
        edit_host_btn = Gtk.Button(label="Edit Host")
        edit_host_btn.set_image(Gtk.Image.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.BUTTON))
        edit_host_btn.set_always_show_image(True)
        
        connect_btn = Gtk.Button(label="Connect")
        connect_btn.set_image(Gtk.Image.new_from_icon_name("network-transmit-symbolic", Gtk.IconSize.BUTTON))
        connect_btn.set_always_show_image(True)
        
        disconnect_btn = Gtk.Button(label="Disconnect")
        disconnect_btn.set_image(Gtk.Image.new_from_icon_name("network-offline-symbolic", Gtk.IconSize.BUTTON))
        disconnect_btn.set_always_show_image(True)
        
        # Connect signals
        add_host_btn.connect("clicked", self.show_add_host_dialog)
        edit_host_btn.connect("clicked", self.show_edit_host_dialog)
        connect_btn.connect("clicked", self.on_connect_clicked)
        disconnect_btn.connect("clicked", self.on_disconnect_clicked)
        
        # Add buttons to box with equal spacing
        host_buttons.pack_start(add_host_btn, True, True, 0)
        host_buttons.pack_start(edit_host_btn, True, True, 0)
        host_buttons.pack_start(connect_btn, True, True, 0)
        host_buttons.pack_start(disconnect_btn, True, True, 0)
        
        # Host list
        scrolled = Gtk.ScrolledWindow()
        self.hosts_list = Gtk.ListBox()
        self.hosts_list.connect("button-press-event", self.on_host_button_press)
        scrolled.add(self.hosts_list)
        
        sidebar.pack_start(scrolled, True, True, 0)
        sidebar.pack_start(host_buttons, False, False, 0)
        
        # Main content area
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        # Service list with descriptions
        self.remote_service_store = Gtk.ListStore(str, str, str, str)  # Name, Status, Host, Description
        self.remote_service_view = Gtk.TreeView(model=self.remote_service_store)
        
        # Create cell renderers with better formatting
        cell = Gtk.CellRendererText()
        cell.set_property("ypad", 6)
        cell.set_property("wrap-mode", Pango.WrapMode.WORD)
        cell.set_property("wrap-width", 300)
        
        # Create columns with better default widths and sorting
        name_col = Gtk.TreeViewColumn("Service")
        name_col.pack_start(cell, True)
        name_col.set_cell_data_func(cell, self.format_service_cell)
        name_col.set_resizable(True)
        name_col.set_expand(True)
        name_col.set_min_width(300)
        name_col.set_sort_column_id(0)  # Sort by service name
        
        status_col = Gtk.TreeViewColumn("Status", Gtk.CellRendererText(), text=1)
        status_col.set_resizable(True)
        status_col.set_fixed_width(120)
        status_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        status_col.set_sort_column_id(1)  # Sort by status
        
        host_col = Gtk.TreeViewColumn("Host", Gtk.CellRendererText(), text=2)
        host_col.set_resizable(True)
        host_col.set_fixed_width(150)
        host_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        host_col.set_sort_column_id(2)  # Sort by host
        
        self.remote_service_view.append_column(name_col)
        self.remote_service_view.append_column(status_col)
        self.remote_service_view.append_column(host_col)
        
        # Connect double-click and context menu
        self.remote_service_view.connect("row-activated", self.on_service_activated)
        self.remote_service_view.connect("button-press-event", self.on_service_button_press)
        
        service_scroll = Gtk.ScrolledWindow()
        service_scroll.add(self.remote_service_view)
        
        # Control buttons with styling
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        # Add a subtle background
        control_box_style = control_box.get_style_context()
        control_box_style.add_class('action-bar')
        
        # Create styled buttons
        start_btn = self.create_action_button("Start", "media-playback-start-symbolic")
        stop_btn = self.create_action_button("Stop", "media-playback-stop-symbolic")
        restart_btn = self.create_action_button("Restart", "view-refresh-symbolic")
        enable_btn = self.create_action_button("Enable", "object-select-symbolic")
        disable_btn = self.create_action_button("Disable", "window-close-symbolic")
        logs_btn = self.create_action_button("View Logs", "text-x-generic-symbolic")
        
        # Connect signals
        start_btn.connect("clicked", self.on_start_service)
        stop_btn.connect("clicked", self.on_stop_service)
        restart_btn.connect("clicked", self.on_restart_service)
        enable_btn.connect("clicked", self.on_enable_service)
        disable_btn.connect("clicked", self.on_disable_service)
        logs_btn.connect("clicked", self.show_remote_logs_dialog)
        
        # Add buttons with consistent spacing
        for btn in (start_btn, stop_btn, restart_btn, enable_btn, disable_btn, logs_btn):
            control_box.pack_start(btn, False, False, 0)
        
        content_box.pack_start(service_scroll, True, True, 0)
        content_box.pack_start(control_box, False, False, 0)
        
        remote_box.pack_start(sidebar, False, False, 0)
        remote_box.pack_start(content_box, True, True, 0)
        
        # Make the store sortable
        self.remote_service_store.set_sort_func(0, self.sort_by_name)
        self.remote_service_store.set_sort_func(1, self.sort_by_status)
        self.remote_service_store.set_sort_func(2, self.sort_by_host)
        
        return remote_box

    def create_local_page(self):
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        # Create service list store and view with descriptions
        self.local_service_store = Gtk.ListStore(str, str, str)  # Name, Status, Description
        self.local_service_view = Gtk.TreeView(model=self.local_service_store)
        
        # Create cell renderers with better formatting
        cell = Gtk.CellRendererText()
        cell.set_property("ypad", 6)
        cell.set_property("wrap-mode", Pango.WrapMode.WORD)
        cell.set_property("wrap-width", 300)
        
        # Create columns with better default widths and sorting
        name_col = Gtk.TreeViewColumn("Service")
        name_col.pack_start(cell, True)
        name_col.set_cell_data_func(cell, self.format_local_service_cell)
        name_col.set_resizable(True)
        name_col.set_expand(True)
        name_col.set_min_width(300)
        name_col.set_sort_column_id(0)  # Sort by service name
        
        status_col = Gtk.TreeViewColumn("Status", Gtk.CellRendererText(), text=1)
        status_col.set_resizable(True)
        status_col.set_fixed_width(120)
        status_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        status_col.set_sort_column_id(1)  # Sort by status
        
        self.local_service_view.append_column(name_col)
        self.local_service_view.append_column(status_col)
        
        # Connect double-click and context menu
        self.local_service_view.connect("row-activated", self.on_local_service_activated)
        self.local_service_view.connect("button-press-event", self.on_local_service_button_press)
        
        # Add scrolled window
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.local_service_view)
        
        # Control buttons with styling
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        # Add a subtle background
        control_box_style = control_box.get_style_context()
        control_box_style.add_class('action-bar')
        
        # Create styled buttons
        start_btn = self.create_action_button("Start", "media-playback-start-symbolic")
        stop_btn = self.create_action_button("Stop", "media-playback-stop-symbolic")
        restart_btn = self.create_action_button("Restart", "view-refresh-symbolic")
        enable_btn = self.create_action_button("Enable", "object-select-symbolic")
        disable_btn = self.create_action_button("Disable", "window-close-symbolic")
        refresh_btn = self.create_action_button("Refresh", "view-refresh-symbolic")
        logs_btn = self.create_action_button("View Logs", "text-x-generic-symbolic")
        
        # Connect signals
        start_btn.connect("clicked", self.on_local_start_service)
        stop_btn.connect("clicked", self.on_local_stop_service)
        restart_btn.connect("clicked", self.on_local_restart_service)
        enable_btn.connect("clicked", self.on_local_enable_service)
        disable_btn.connect("clicked", self.on_local_disable_service)
        refresh_btn.connect("clicked", self.refresh_local_services)
        logs_btn.connect("clicked", self.show_local_logs_dialog)
        
        # Add buttons with consistent spacing
        for btn in (start_btn, stop_btn, restart_btn, enable_btn, disable_btn, refresh_btn, logs_btn):
            control_box.pack_start(btn, False, False, 0)
        
        local_box.pack_start(scrolled, True, True, 0)
        local_box.pack_start(control_box, False, False, 0)
        
        # Initial service load
        self.refresh_local_services()
        
        # Make the store sortable
        self.local_service_store.set_sort_func(0, self.sort_by_name)
        self.local_service_store.set_sort_func(1, self.sort_by_status)
        
        return local_box

    def create_action_button(self, label, icon_name=None):
        """Create a styled action button"""
        btn = Gtk.Button(label=label)
        btn.set_always_show_image(True)
        
        if icon_name:
            image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
            btn.set_image(image)
            btn.set_image_position(Gtk.PositionType.TOP)
        
        # Add button styling
        btn_style = btn.get_style_context()
        btn_style.add_class('action-button')
        
        return btn

    def refresh_local_services(self, widget=None):
        """Refresh the list of local systemd services asynchronously"""
        # Show a loading indicator
        self.local_service_store.clear()
        self.local_service_store.append(["Loading services...", "", "Please wait"])
        
        # Run the actual refresh in a background thread
        thread = threading.Thread(target=self._refresh_local_services_thread)
        thread.daemon = True
        thread.start()
        return False

    def _refresh_local_services_thread(self):
        """Background thread to refresh local services"""
        try:
            # Get all unit files in one call
            unit_files_cmd = ["systemctl", "list-unit-files", "--type=service", "--output=json", "--no-pager"]
            unit_files_result = subprocess.run(unit_files_cmd, capture_output=True, text=True)
            
            # Get all active statuses in one call
            active_cmd = ["systemctl", "list-units", "--type=service", "--state=active,running,failed", "--output=json", "--no-pager"]
            active_result = subprocess.run(active_cmd, capture_output=True, text=True)
            
            # Parse JSON output
            import json
            unit_files = json.loads(unit_files_result.stdout)
            
            # Create a dictionary of active services for quick lookup
            active_services = {}
            try:
                active_list = json.loads(active_result.stdout)
                for service in active_list:
                    name = service.get("unit", "")
                    active_state = service.get("active", "")
                    sub_state = service.get("sub", "")
                    description = service.get("description", "")
                    active_services[name] = {
                        "status": f"{active_state} ({sub_state})",
                        "description": description
                    }
            except Exception as e:
                self.logger.error(f"Error parsing active services: {str(e)}")
            
            # Prepare the data for the store
            services_data = []
            services_needing_description = []
            
            for unit_file in unit_files:
                unit_name = unit_file.get("unit_file", "")
                state = unit_file.get("state", "")
                
                if unit_name.endswith(".service"):
                    # Use active status if available, otherwise use unit file state
                    if unit_name in active_services:
                        status = active_services[unit_name]["status"]
                        description = active_services[unit_name]["description"]
                    else:
                        status = state
                        description = ""
                        services_needing_description.append(unit_name)
                    
                    services_data.append([unit_name, status, description])
            
            # Fetch descriptions for services that need them - one by one for reliability
            for service_name in services_needing_description:
                try:
                    # Try to get description using 'systemctl show' command
                    desc_cmd = ["systemctl", "show", "--property=Description", service_name]
                    desc_result = subprocess.run(desc_cmd, capture_output=True, text=True)
                    desc_output = desc_result.stdout.strip()
                    
                    if desc_output and "=" in desc_output:
                        description = desc_output.split("=", 1)[1].strip()
                        
                        # If that fails, try to get it from the unit file directly
                        if not description:
                            # Try to read from unit file
                            unit_file_cmd = ["cat", f"/usr/lib/systemd/system/{service_name}", f"/etc/systemd/system/{service_name}"]
                            unit_file_result = subprocess.run(unit_file_cmd, capture_output=True, text=True)
                            unit_file_content = unit_file_result.stdout
                            
                            # Extract Description from unit file
                            import re
                            desc_match = re.search(r'Description=(.+)', unit_file_content)
                            if desc_match:
                                description = desc_match.group(1).strip()
                        
                        # Update the description in our data
                        for service in services_data:
                            if service[0] == service_name:
                                service[2] = description
                                break
                except Exception as e:
                    self.logger.debug(f"Could not get description for {service_name}: {str(e)}")
                    # Continue with next service - don't let one failure stop the process
            
            # Update the UI in the main thread
            GLib.idle_add(self._update_local_services_store, services_data)
            
        except json.JSONDecodeError as e:
            GLib.idle_add(lambda: self.show_error_dialog(f"Failed to parse service list: {str(e)}"))
        except Exception as e:
            GLib.idle_add(lambda: self.show_error_dialog(f"Failed to refresh local services: {str(e)}"))

    def _update_local_services_store(self, services_data):
        """Update the service store with the fetched data"""
        self.local_service_store.clear()
        for service in services_data:
            self.local_service_store.append(service)
        
        # Update statusbar with fresh data
        self.update_statusbar()
        return False

    def show_add_host_dialog(self, button):
        dialog = Gtk.Dialog(
            title="Add Remote Host",
            parent=self,
            flags=0
        )
        
        dialog.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content = dialog.get_content_area()
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(6)
        
        # Input fields
        name_entry = Gtk.Entry()
        host_entry = Gtk.Entry()
        username_entry = Gtk.Entry()
        password_entry = Gtk.Entry()
        password_entry.set_visibility(False)  # Hide password characters
        
        auth_combo = Gtk.ComboBoxText()
        auth_combo.append_text("Password")
        auth_combo.append_text("SSH Key")
        auth_combo.set_active(0)
        
        key_chooser = Gtk.FileChooserButton(title="Select SSH Key")
        key_chooser.set_sensitive(False)
        
        # Make password/key fields sensitive based on auth type
        def on_auth_changed(combo):
            is_password = combo.get_active_text() == "Password"
            password_entry.set_sensitive(is_password)
            key_chooser.set_sensitive(not is_password)
        
        auth_combo.connect("changed", on_auth_changed)
        
        # Layout
        labels = ["Name:", "Hostname:", "Username:", "Password:", "Auth Type:", "SSH Key:"]
        widgets = [name_entry, host_entry, username_entry, password_entry, auth_combo, key_chooser]
        
        for i, (label, widget) in enumerate(zip(labels, widgets)):
            grid.attach(Gtk.Label(label=label), 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
        
        content.add(grid)
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            host = RemoteHost(
                name=name_entry.get_text(),
                hostname=host_entry.get_text(),
                username=username_entry.get_text(),
                auth_type="key" if auth_combo.get_active_text() == "SSH Key" else "password",
                key_path=key_chooser.get_filename() if auth_combo.get_active_text() == "SSH Key" else None
            )
            # Store password in keyring if using password auth
            if host.auth_type == "password":
                keyring.set_password(
                    "systemd-manager",
                    f"{host.username}@{host.hostname}",
                    password_entry.get_text()
            )
            self.add_remote_host(host)
        
        dialog.destroy()

    def add_remote_host(self, host: RemoteHost):
        """Add a new remote host without connecting"""
        self.remote_hosts[host.name] = host
        self.save_hosts()
        self.refresh_hosts_list()
        # No automatic connection or refresh here

    def on_connect_clicked(self, button):
        """Handle connect button click"""
        selection = self.hosts_list.get_selected_row()
        if not selection:
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        host = self.remote_hosts.get(host_name)
        if not host:
            return
        
        # Show connecting dialog with spinner
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CANCEL,
            text=f"Connecting to {host.name}..."
        )
        
        spinner = Gtk.Spinner()
        spinner.start()
        dialog.get_content_area().pack_end(spinner, False, False, 10)
        dialog.show_all()
        
        def do_connect():
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                # Connect with timeout
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    password=keyring.get_password("systemd-manager", f"{host.username}@{host.hostname}") if host.auth_type == "password" else None,
                    key_filename=host.key_path if host.auth_type == "key" else None,
                    timeout=5  # 5 second timeout
                )
                
                # Test connection with a simple command
                stdin, stdout, stderr = client.exec_command("echo test", timeout=5)
                if stdout.read().decode().strip() != "test":
                    GLib.idle_add(lambda: self.on_connect_error("Failed to execute test command", dialog))
                    return None
                
                GLib.idle_add(lambda: self.on_connect_success(host.name, client, dialog))
                
                return client
            except Exception as e:
                self.logger.error(f"Connection failed: {str(e)}")
                return None
        
        # Run connection in a thread
        thread = threading.Thread(target=do_connect)
        thread.daemon = True
        thread.start()
        
        # Handle dialog response (Cancel button)
        response = dialog.run()
        if response == Gtk.ResponseType.CANCEL:
            dialog.destroy()

    def on_connect_success(self, host_name, client, dialog):
        """Handle successful connection"""
        self.active_connections[host_name] = client
        self.logger.info(f"Successfully connected to {host_name}")
        dialog.destroy()
        
        # Update UI
        self.refresh_hosts_list()
        # Find and select the row for this host
        for row in self.hosts_list.get_children():
            host_label = row.get_children()[0].get_children()[1]
            if host_label.get_text() == host_name:
                self.hosts_list.select_row(row)
                # Refresh services for the newly connected host
                self.refresh_services(host_name)
                break
        return False

    def on_connect_error(self, error_msg, dialog):
        """Handle connection error"""
        dialog.destroy()
        self.show_error_dialog(f"Connection failed: {error_msg}")
        return False

    def _create_ssh_client(self, host: RemoteHost) -> paramiko.SSHClient:
        """Create and configure SSH client"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            if host.auth_type == "password":
                password = keyring.get_password("systemd-manager", f"{host.username}@{host.hostname}")
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    password=password,
                    timeout=10
                )
                
                # Configure sudo to not require tty
                client.exec_command("echo 'Defaults:$USER !requiretty' | sudo -S tee /etc/sudoers.d/systemd-manager")
                
            else:
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    key_filename=host.key_path,
                    timeout=10
                )
                
                # Configure sudo to not require tty
                client.exec_command("echo 'Defaults:$USER !requiretty' | sudo -S tee /etc/sudoers.d/systemd-manager")
            
        except Exception as e:
            client.close()
            raise e
        
        return client

    def save_hosts(self):
        config_dir = Path.home() / ".config" / "systemd-manager"
        config_dir.mkdir(parents=True, exist_ok=True)
        
        hosts_data = {
            name: {
                "name": host.name,
                "hostname": host.hostname,
                "username": host.username,
                "auth_type": host.auth_type,
                "key_path": host.key_path
            }
            for name, host in self.remote_hosts.items()
        }
        
        with open(config_dir / "hosts.json", "w") as f:
            json.dump(hosts_data, f)

    def load_saved_hosts(self):
        config_file = Path.home() / ".config" / "systemd-manager" / "hosts.json"
        if not config_file.exists():
            return
            
        with open(config_file) as f:
            hosts_data = json.load(f)
            
        for host_data in hosts_data.values():
            host = RemoteHost(**host_data)
            self.remote_hosts[host.name] = host
            
        self.refresh_hosts_list()

    def refresh_hosts_list(self):
        """Refresh the list of hosts and their connection status"""
        for child in self.hosts_list.get_children():
            self.hosts_list.remove(child)
            
        for host in self.remote_hosts.values():
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            
            label = Gtk.Label(label=host.name)
            status = Gtk.Image()
            
            # Update status icon based on connection state
            if host.name in self.active_connections:
                status.set_from_icon_name("gtk-yes", Gtk.IconSize.SMALL_TOOLBAR)
            else:
                status.set_from_icon_name("gtk-no", Gtk.IconSize.SMALL_TOOLBAR)
            
            box.pack_start(status, False, False, 0)
            box.pack_start(label, True, True, 0)
            row.add(box)
            self.hosts_list.add(row)
            
        self.hosts_list.show_all()

    def refresh_services(self, host_name):
        """Refresh services for a remote host asynchronously"""
        client = self.active_connections.get(host_name)
        if not client:
            return False
        
        # Show a loading indicator
        self.remote_service_store.clear()
        self.remote_service_store.append(["Loading services...", "", host_name, "Please wait"])
        
        # Run the actual refresh in a background thread
        thread = threading.Thread(target=self._refresh_services_thread, args=(host_name,))
        thread.daemon = True
        thread.start()
        
        # Update statusbar after refresh
        GLib.idle_add(self.update_statusbar)
        return False

    def _refresh_services_thread(self, host_name):
        """Background thread to refresh remote services"""
        client = self.active_connections.get(host_name)
        if not client:
            return
        
        try:
            # Get all unit files in one call
            unit_files_cmd = "systemctl list-unit-files --type=service --output=json --no-pager"
            stdin, stdout, stderr = client.exec_command(unit_files_cmd)
            unit_files_output = stdout.read().decode()
            
            # Get all active statuses in one call
            active_cmd = "systemctl list-units --type=service --state=active,running,failed --output=json --no-pager"
            stdin, stdout, stderr = client.exec_command(active_cmd)
            active_output = stdout.read().decode()
            
            # Parse JSON output
            import json
            unit_files = json.loads(unit_files_output)
            
            # Create a dictionary of active services for quick lookup
            active_services = {}
            try:
                active_list = json.loads(active_output)
                for service in active_list:
                    name = service.get("unit", "")
                    active_state = service.get("active", "")
                    sub_state = service.get("sub", "")
                    description = service.get("description", "")
                    active_services[name] = {
                        "status": f"{active_state} ({sub_state})",
                        "description": description
                    }
            except Exception as e:
                self.logger.error(f"Error parsing active services: {str(e)}")
            
            # Prepare the data for the store
            services_data = []
            
            for unit_file in unit_files:
                unit_name = unit_file.get("unit_file", "")
                state = unit_file.get("state", "")
                
                if unit_name.endswith(".service"):
                    # Use active status if available, otherwise use unit file state
                    if unit_name in active_services:
                        status = active_services[unit_name]["status"]
                        description = active_services[unit_name]["description"]
                    else:
                        status = state
                        description = ""
                    
                    services_data.append([unit_name, status, host_name, description])
            
            # Update the UI in the main thread
            GLib.idle_add(self._update_remote_services_store, services_data)
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse service list: {str(e)}")
        except Exception as e:
            self.logger.error(f"Failed to refresh services: {str(e)}")

    def _update_remote_services_store(self, services_data):
        """Update the remote service store with the fetched data"""
        self.remote_service_store.clear()
        for service in services_data:
            self.remote_service_store.append(service)
        return False

    def show_local_service_details(self, service_name):
        """Show detailed information for local service"""
        dialog = Gtk.Dialog(
            title=f"Service Details - {service_name.replace('.service', '')}",
            parent=self,
            flags=0
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(600, 400)
        
        content = dialog.get_content_area()
        
        # Add status header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(12)
        header_box.set_margin_bottom(12)
        
        try:
            # Get enabled status
            result = subprocess.run(
                ["systemctl", "is-enabled", service_name],
                capture_output=True,
                text=True
            )
            enabled_status = result.stdout.strip()
            
            # Get active status
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True
            )
            active_status = result.stdout.strip()
            
            # Create status labels with colored backgrounds
            enabled_label = Gtk.Label()
            enabled_label.set_markup(
                f'<span background="{self.get_status_color(enabled_status)}" foreground="white">'
                f' {enabled_status.upper()} </span>'
            )
            
            active_label = Gtk.Label()
            active_label.set_markup(
                f'<span background="{self.get_status_color(active_status)}" foreground="white">'
                f' {active_status.upper()} </span>'
            )
            
            header_box.pack_start(Gtk.Label(label="Status:"), False, False, 0)
            header_box.pack_start(active_label, False, False, 0)
            header_box.pack_start(Gtk.Label(label="Boot Status:"), False, False, 0)
            header_box.pack_start(enabled_label, False, False, 0)
            
        except Exception as e:
            self.logger.error(f"Failed to get service status: {str(e)}")
        
        content.pack_start(header_box, False, False, 0)
        
        # Add notebook with details
        notebook = Gtk.Notebook()
        
        # Status page
        status_view = self.create_log_text_view()
        status_scroll = Gtk.ScrolledWindow()
        status_scroll.add(status_view)
        notebook.append_page(status_scroll, Gtk.Label(label="Status"))
        
        # Properties page
        props_view = self.create_log_text_view()
        props_scroll = Gtk.ScrolledWindow()
        props_scroll.add(props_view)
        notebook.append_page(props_scroll, Gtk.Label(label="Properties"))
        
        content.pack_start(notebook, True, True, 0)
        
        try:
            # Get status
            result = subprocess.run(
                ["systemctl", "status", service_name],
                capture_output=True,
                text=True
            )
            status_view.get_buffer().set_text(result.stdout)
            
            # Get properties
            result = subprocess.run(
                ["systemctl", "show", service_name],
                capture_output=True,
                text=True
            )
            props_view.get_buffer().set_text(result.stdout)
            
        except Exception as e:
            self.show_error_dialog(f"Failed to fetch service details: {str(e)}")
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def sort_by_name(self, model, iter1, iter2, user_data):
        """Sort services by name"""
        name1 = model.get_value(iter1, 0)
        name2 = model.get_value(iter2, 0)
        return (name1 > name2) - (name1 < name2)

    def sort_by_status(self, model, iter1, iter2, user_data):
        """Sort services by status"""
        status1 = model.get_value(iter1, 1)
        status2 = model.get_value(iter2, 1)
        return (status1 > status2) - (status1 < status2)

    def sort_by_host(self, model, iter1, iter2, user_data):
        """Sort services by host"""
        host1 = model.get_value(iter1, 2)
        host2 = model.get_value(iter2, 2)
        return (host1 > host2) - (host1 < host2)

    def format_status_output(self, text):
        """Format systemctl status output with Pango markup"""
        formatted_lines = []
        for line in text.splitlines():
            # Escape any existing markup
            line = GLib.markup_escape_text(line)
            
            # Highlight service name and description
            if "Loaded:" in line:
                line = line.replace(
                    "loaded",
                    '<span foreground="#2ecc71">loaded</span>'  # Green
                )
            elif "Active:" in line:
                if "active (running)" in line.lower():
                    line = line.replace(
                        "active",
                        '<span foreground="#2ecc71">active</span>'  # Green
                    )
                elif "inactive" in line.lower():
                    line = line.replace(
                        "inactive",
                        '<span foreground="#e74c3c">inactive</span>'  # Red
                    )
                elif "failed" in line.lower():
                    line = line.replace(
                        "failed",
                        '<span foreground="#e74c3c">failed</span>'  # Red
                    )
            elif "Main PID:" in line:
                line = f'<span foreground="#3498db">{line}</span>'  # Blue
            elif "CGroup:" in line:
                line = f'<span foreground="#f1c40f">{line}</span>'  # Yellow
            
            formatted_lines.append(line)
        
        return '<span font_family="monospace">' + '\n'.join(formatted_lines) + '</span>'

    def on_disconnect_clicked(self, button):
        """Handle disconnect button click"""
        selection = self.hosts_list.get_selected_row()
        if not selection:
            self.show_error_dialog("Please select a host to disconnect")
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        client = self.active_connections.get(host_name)
        
        if not client:
            self.show_error_dialog("Host is not connected")
            return
        
        try:
            client.close()
            del self.active_connections[host_name]
            self.refresh_hosts_list()  # Update the host list to show disconnected state
            self.remote_service_store.clear()  # Clear the services list
            self.show_info_dialog(f"Disconnected from {host_name}")
            
        except Exception as e:
            self.show_error_dialog(f"Failed to disconnect: {str(e)}")

    def toggle_theme(self, button):
        """Toggle between light and dark theme"""
        self.is_dark_mode = not self.is_dark_mode
        self.settings.set_property("gtk-application-prefer-dark-theme", self.is_dark_mode)
        
        # Update CSS for custom widgets if needed
        self.update_custom_theme()

    def update_custom_theme(self):
        """Update custom widget themes based on current mode"""
        # Common CSS for both themes - static black background for text views
        css_data = b"""
            .action-bar {
                border-top: 1px solid alpha(#000000, 0.1);
                padding: 6px;
            }
            
            .action-button {
                padding: 8px;
                margin: 4px;
                border-radius: 4px;
                min-width: 80px;
            }
            
            .action-button:hover {
                background-color: alpha(#000000, 0.1);
            }
            
            /* Always black background for text content */
            .theme-text {
                color: #ffffff;
                background-color: #000000;
                padding: 10px;
                font-family: monospace;
            }
            
            .theme-background {
                background-color: #000000;
                padding: 6px;
            }
            
            textview {
                color: #ffffff;
                background-color: #000000;
                font-family: monospace;
            }
            
            textview text {
                color: #ffffff;
                background-color: #000000;
            }
            
            scrolledwindow {
                background-color: #000000;
            }
        """
        
        self.css_provider.load_from_data(css_data)

    def show_about_dialog(self, button):
        """Show the About dialog"""
        about_dialog = Gtk.AboutDialog(transient_for=self)
        about_dialog.set_modal(True)
        
        # Set dialog properties
        about_dialog.set_program_name(APP_NAME)
        about_dialog.set_version(APP_VERSION)
        about_dialog.set_comments(APP_DESCRIPTION)
        about_dialog.set_authors(APP_AUTHORS)
        about_dialog.set_website(APP_WEBSITE)
        about_dialog.set_website_label("Project website")
        about_dialog.set_license_type(Gtk.License.GPL_3_0)
        about_dialog.set_logo_icon_name(None)  # Try this instead
        about_dialog.set_logo(None)
        about_dialog.set_icon(None)
        
        about_dialog.connect("response", lambda d, r: d.destroy())
        about_dialog.show()

    def create_dark_source_view(self, language_id=None):
        """Create a dark themed source view with syntax highlighting"""
        scrolled = Gtk.ScrolledWindow()
        
        # Set up source view with better editing features
        source_buffer = GtkSource.Buffer()
        if language_id:
            source_buffer.set_language(
                GtkSource.LanguageManager.get_default().get_language(language_id)
            )
        
        # Use default style scheme for syntax highlighting
        style_manager = GtkSource.StyleSchemeManager.get_default()
        style_scheme = style_manager.get_scheme('classic')  # Use classic scheme for black on white
        source_buffer.set_style_scheme(style_scheme)
        
        source_view = GtkSource.View.new_with_buffer(source_buffer)
        source_view.set_show_line_numbers(True)
        source_view.set_auto_indent(True)
        source_view.set_indent_width(2)
        source_view.set_insert_spaces_instead_of_tabs(True)
        source_view.set_highlight_current_line(True)
        source_view.set_monospace(True)
        
        # Force black on white colors
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            textview {
                color: #000000;
                background-color: #ffffff;
            }
            textview text {
                color: #000000;
                background-color: #ffffff;
            }
        """)
        
        source_view.get_style_context().add_provider(
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        scrolled.add(source_view)
        return scrolled, source_view

    def toggle_show_inactive(self, button):
        """Toggle showing inactive services"""
        self.show_inactive = not self.show_inactive
        # Update both local and remote service lists
        self.refresh_local_services()
        if self.stack.get_visible_child_name() == "remote":
            selection = self.hosts_list.get_selected_row()
            if selection:
                host_name = selection.get_children()[0].get_children()[1].get_text()
                if host_name in self.active_connections:
                    self.refresh_services(host_name)

    def show_create_service_dialog(self, button):
        """Show dialog to create a new systemd service"""
        # Check if we're on the remote page and a host is connected
        is_remote = self.stack.get_visible_child_name() == "remote"
        if is_remote:
            selection = self.hosts_list.get_selected_row()
            if not selection:
                self.show_error_dialog("Please select a remote host")
                return
            
            host_name = selection.get_children()[0].get_children()[1].get_text()
            client = self.active_connections.get(host_name)
            if not client:
                self.show_error_dialog("Please connect to the remote host first")
                return
        
        # Create dialog
        dialog = Gtk.Dialog(
            title="Create Service",
            parent=self,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dialog.set_default_size(700, 500)
        
        content = dialog.get_content_area()
        content.set_spacing(6)
        
        # Service name field at the top
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_box.set_margin_start(12)
        name_box.set_margin_end(12)
        name_box.set_margin_top(12)
        
        name_label = Gtk.Label(label="Service Name:")
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("my-service")
        
        name_box.pack_start(name_label, False, False, 0)
        name_box.pack_start(name_entry, True, True, 0)
        
        content.add(name_box)
        
        # Create source view for editing
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_margin_start(12)
        scrolled.set_margin_end(12)
        scrolled.set_margin_bottom(12)
        
        # Set up the source view
        source_buffer = GtkSource.Buffer()
        source_buffer.set_language(
            GtkSource.LanguageManager.get_default().get_language('ini')
        )
        source_buffer.set_style_scheme(
            GtkSource.StyleSchemeManager.get_default().get_scheme('oblivion')
        )
        
        source_view = GtkSource.View.new_with_buffer(source_buffer)
        source_view.set_show_line_numbers(True)
        source_view.set_auto_indent(True)
        source_view.set_tab_width(4)
        source_view.set_indent_width(4)
        source_view.set_insert_spaces_instead_of_tabs(True)
        source_view.set_smart_backspace(True)
        
        # Set default template
        template = """[Unit]
Description=My custom service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/sleep infinity

[Install]
WantedBy=multi-user.target
"""
        source_buffer.set_text(template)
        
        scrolled.add(source_view)
        content.pack_start(scrolled, True, True, 0)
        
        dialog.show_all()
        
        while True:
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                service_name = name_entry.get_text()
                if not service_name:
                    self.show_error_dialog("Please enter a service name")
                    continue
                
                if not service_name.endswith('.service'):
                    service_name += '.service'
                
                service_content = source_buffer.get_text(
                    source_buffer.get_start_iter(),
                    source_buffer.get_end_iter(),
                    True
                )
                
                try:
                    if is_remote:
                        self.logger.info(f"Creating remote service {service_name}")
                        
                        # Check if we're root first
                        stdin, stdout, stderr = client.exec_command("id -u")
                        uid = stdout.read().decode().strip()
                        is_root = (uid == "0")
                        
                        if not is_root:
                            # Use the existing better sudo dialog
                            success, sudo_password = self.show_sudo_password_dialog(
                                host=host_name,
                                command=f"Create and configure service: {service_name}"
                            )
                            if not success:
                                continue
                            
                            # Function to run sudo command with saved password
                            def run_sudo_command(cmd):
                                full_cmd = f"echo '{sudo_password}' | sudo -S {cmd}"
                                stdin, stdout, stderr = client.exec_command(full_cmd)
                                error = stderr.read().decode()
                                output = stdout.read().decode()
                                return output, error
                        else:
                            # We're root, no need for sudo
                            def run_sudo_command(cmd):
                                stdin, stdout, stderr = client.exec_command(cmd)
                                error = stderr.read().decode()
                                output = stdout.read().decode()
                                return output, error
                        
                        # Create service file and write content
                        self.logger.debug("Creating service file and writing content")
                        temp_file = f"/tmp/{service_name}"
                        cmd = f"echo '{service_content}' > {temp_file}"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error:
                            self.logger.error(f"Failed to create temp file: {error}")
                            self.show_error_dialog(f"Failed to create temp file: {error}")
                            continue
                            
                        # Move file to systemd directory
                        cmd = f"mv {temp_file} /etc/systemd/system/" if is_root else f"echo '{sudo_password}' | sudo -S mv {temp_file} /etc/systemd/system/"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and (not is_root and not "password" in error.lower()):
                            self.logger.error(f"Failed to move service file: {error}")
                            self.show_error_dialog(f"Failed to move service file: {error}")
                            continue
                            
                        # Set proper permissions
                        self.logger.debug("Setting file permissions")
                        cmd = f"chmod 644 /etc/systemd/system/{service_name}" if is_root else f"echo '{sudo_password}' | sudo -S chmod 644 /etc/systemd/system/{service_name}"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and (not is_root and not "password" in error.lower()):
                            self.logger.error(f"Failed to set permissions: {error}")
                            self.show_error_dialog(f"Failed to set permissions: {error}")
                            continue
                        
                        # Verify the file exists
                        cmd = f"test -f /etc/systemd/system/{service_name} && echo 'exists'"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        output = stdout.read().decode().strip()
                        
                        if output != 'exists':
                            self.logger.error("Service file was not created")
                            self.show_error_dialog("Failed to create service file")
                            continue
                            
                        # Reload systemd
                        self.logger.debug("Reloading systemd")
                        cmd = f"systemctl daemon-reload" if is_root else f"echo '{sudo_password}' | sudo -S systemctl daemon-reload"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and not "password" in error.lower():
                            self.logger.error(f"Failed to reload systemd: {error}")
                            self.show_error_dialog(f"Failed to reload systemd: {error}")
                            continue
                            
                    else:
                        # Get sudo password once for all local operations
                        success, sudo_password = self.show_sudo_password_dialog(
                            command=f"Create and configure service: {service_name}"
                        )
                        if not success:
                            continue
                        
                        # Helper function for local sudo commands
                        def run_local_sudo_command(cmd, input_data=None):
                            process = subprocess.Popen(
                                ["sudo", "-S"] + cmd,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True
                            )
                            
                            if input_data:
                                stdout, stderr = process.communicate(f"{sudo_password}\n{input_data}")
                            else:
                                stdout, stderr = process.communicate(sudo_password + "\n")
                            
                            return process.returncode, stdout, stderr
                        
                        # Create service file
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["tee", f"/etc/systemd/system/{service_name}"],
                            service_content
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to create service file: {stderr}")
                            continue
                        
                        # Set proper permissions
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["chmod", "644", f"/etc/systemd/system/{service_name}"]
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to set permissions: {stderr}")
                            continue
                        
                        # Reload systemd
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["systemctl", "daemon-reload"]
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to reload systemd: {stderr}")
                            continue
                    
                    # Ask if the service should be started (moved outside the if/else)
                    start_dialog = Gtk.MessageDialog(
                        transient_for=self,
                        flags=0,
                        message_type=Gtk.MessageType.QUESTION,
                        buttons=Gtk.ButtonsType.YES_NO,
                        text="Service Created"
                    )
                    start_dialog.format_secondary_text(
                        f"Service {service_name} was created successfully. Would you like to start it now?"
                    )
                    
                    start_response = start_dialog.run()
                    start_dialog.destroy()
                    
                    if start_response == Gtk.ResponseType.YES:
                        if is_remote:
                            output, error = run_sudo_command(f"systemctl start {service_name}")
                            if error and not "password" in error.lower():
                                self.logger.error(f"Failed to start service: {error}")
                                self.show_error_dialog(f"Failed to start service: {error}")
                            else:
                                self.show_info_dialog(f"Service {service_name} started successfully")
                        else:
                            returncode, stdout, stderr = run_local_sudo_command(
                                ["systemctl", "start", service_name]
                            )
                            if returncode == 0:
                                self.show_info_dialog(f"Service {service_name} started successfully")
                            else:
                                self.show_error_dialog(f"Failed to start service: {stderr}")
                    
                    # Refresh the appropriate service list
                    if is_remote:
                        GLib.idle_add(lambda: self.refresh_services(host_name))
                    else:
                        GLib.idle_add(self.refresh_local_services)
                    
                    break
                    
                except Exception as e:
                    self.show_error_dialog(f"Failed to create service: {str(e)}")
                    continue
            else:
                break
        
        dialog.destroy()

    def reload_systemd_config(self, button=None):
        """Reload systemd configuration"""
        is_remote = self.stack.get_visible_child_name() == "remote"
        
        if is_remote:
            # Get selected remote host
            selection = self.hosts_list.get_selected_row()
            if not selection:
                self.show_error_dialog("Please select a remote host")
                return
            
            host_name = selection.get_children()[0].get_children()[1].get_text()
            client = self.active_connections.get(host_name)
            if not client:
                self.show_error_dialog("Please connect to the remote host first")
                return
            
            try:
                success, sudo_password = self.show_sudo_password_dialog(
                    host=host_name,
                    command="systemctl daemon-reload"
                )
                
                if not success:
                    return
                    
                # Execute remote reload
                cmd = f"echo '{sudo_password}' | sudo -S systemctl daemon-reload"
                stdin, stdout, stderr = client.exec_command(cmd)
                error = stderr.read().decode()
                
                if error and not "password" in error.lower():
                    self.show_error_dialog(f"Failed to reload configuration: {error}")
                else:
                    self.show_info_dialog("Systemd configuration reloaded")
                    self.refresh_services(host_name)
            except Exception as e:
                self.show_error_dialog(f"Failed to reload configuration: {str(e)}")
        else:
            # Local reload
            try:
                subprocess.run(["pkexec", "systemctl", "daemon-reload"], check=True)
                self.show_info_dialog("Systemd configuration reloaded")
                self.refresh_local_services()
            except subprocess.CalledProcessError as e:
                self.show_error_dialog(f"Failed to reload configuration: {str(e)}")

    def format_local_service_cell(self, column, cell, model, iter, data):
        """Format service name and description in a single cell"""
        try:
            name = model[iter][0].replace(".service", "")
            description = model[iter][2]  # Description is in column 2 for local services
            
            # Escape special characters for markup
            if description:
                description = GLib.markup_escape_text(description)
                markup = f'<b>{name}</b>\n<span size="smaller" style="italic">{description}</span>'
            else:
                markup = f'<b>{name}</b>'
                
            cell.set_property("markup", markup)
            
        except Exception as e:
            self.logger.error(f"Error formatting service cell: {str(e)}")
            cell.set_property("text", model.get_value(iter, 0))

    def on_local_service_activated(self, treeview, path, column):
        """Handle double-click on a local service"""
        model = treeview.get_model()
        iter = model.get_iter(path)
        service_name = model[iter][0]
        
        # Show service details dialog
        self.show_local_service_details(service_name)

    def on_local_service_button_press(self, treeview, event):
        """Handle right-click on a local service"""
        if event.button == 3:  # Right mouse button
            # Get the service at the clicked position
            path_info = treeview.get_path_at_pos(int(event.x), int(event.y))
            if not path_info:
                return False
            
            path, column, cell_x, cell_y = path_info
            model = treeview.get_model()
            iter = model.get_iter(path)
            service_name = model[iter][0]
            
            # Create popup menu
            menu = Gtk.Menu()
            
            # Add menu items
            start_item = Gtk.MenuItem(label="Start")
            stop_item = Gtk.MenuItem(label="Stop")
            restart_item = Gtk.MenuItem(label="Restart")
            enable_item = Gtk.MenuItem(label="Enable")
            disable_item = Gtk.MenuItem(label="Disable")
            logs_item = Gtk.MenuItem(label="View Logs")
            
            # Connect signals
            start_item.connect("activate", lambda w: self.on_local_start_service(None, service_name))
            stop_item.connect("activate", lambda w: self.on_local_stop_service(None, service_name))
            restart_item.connect("activate", lambda w: self.on_local_restart_service(None, service_name))
            enable_item.connect("activate", lambda w: self.on_local_enable_service(None, service_name))
            disable_item.connect("activate", lambda w: self.on_local_disable_service(None, service_name))
            logs_item.connect("activate", lambda w: self.show_local_logs_dialog(None, service_name))
            
            # Add items to menu
            menu.append(start_item)
            menu.append(stop_item)
            menu.append(restart_item)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(enable_item)
            menu.append(disable_item)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(logs_item)
            
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        
        return False

    def on_local_start_service(self, button, service_name=None):
        """Start a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to start")
                return
            service_name = model[iter][0]
        
        try:
            # Use pkexec for privilege escalation
            subprocess.run(["pkexec", "systemctl", "start", service_name], check=True)
            self.show_info_dialog(f"Service {service_name} started")
            # Refresh the service list
            GLib.timeout_add(1000, self.refresh_local_services)
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to start service: {e}")

    def on_local_stop_service(self, button, service_name=None):
        """Stop a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to stop")
                return
            service_name = model[iter][0]
        
        try:
            # Use pkexec for privilege escalation
            subprocess.run(["pkexec", "systemctl", "stop", service_name], check=True)
            self.show_info_dialog(f"Service {service_name} stopped")
            # Refresh the service list
            GLib.timeout_add(1000, self.refresh_local_services)
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to stop service: {e}")

    def on_local_restart_service(self, button, service_name=None):
        """Restart a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to restart")
                return
            service_name = model[iter][0]
        
        try:
            # Use pkexec for privilege escalation
            subprocess.run(["pkexec", "systemctl", "restart", service_name], check=True)
            self.show_info_dialog(f"Service {service_name} restarted")
            # Refresh the service list
            GLib.timeout_add(1000, self.refresh_local_services)
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to restart service: {e}")

    def on_local_enable_service(self, button, service_name=None):
        """Enable a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to enable")
                return
            service_name = model[iter][0]
        
        try:
            # Use pkexec for privilege escalation
            subprocess.run(["pkexec", "systemctl", "enable", service_name], check=True)
            self.show_info_dialog(f"Service {service_name} enabled")
            # Refresh the service list
            GLib.timeout_add(1000, self.refresh_local_services)
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to enable service: {e}")

    def on_local_disable_service(self, button, service_name=None):
        """Disable a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to disable")
                return
            service_name = model[iter][0]
        
        try:
            # Use pkexec for privilege escalation
            subprocess.run(["pkexec", "systemctl", "disable", service_name], check=True)
            self.show_info_dialog(f"Service {service_name} disabled")
            # Refresh the service list
            GLib.timeout_add(1000, self.refresh_local_services)
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to disable service: {e}")

    def show_local_logs_dialog(self, button, service_name=None):
        """Show logs for a local service"""
        if not service_name:
            # Get selected service from treeview
            selection = self.local_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to view logs")
                return
            service_name = model[iter][0]
        
        # Create dialog
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name.replace('.service', '')}",
            parent=self,
            flags=0
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(800, 500)
        
        content = dialog.get_content_area()
        
        # Create log view
        log_view = self.create_log_text_view()
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(log_view)
        
        # Add controls
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        lines_label = Gtk.Label(label="Lines:")
        lines_spin = Gtk.SpinButton.new_with_range(10, 1000, 10)
        lines_spin.set_value(100)
        
        refresh_btn = Gtk.Button(label="Refresh")
        follow_check = Gtk.CheckButton(label="Follow")
        
        control_box.pack_start(lines_label, False, False, 0)
        control_box.pack_start(lines_spin, False, False, 0)
        control_box.pack_start(refresh_btn, False, False, 0)
        control_box.pack_start(follow_check, False, False, 0)
        
        content.pack_start(control_box, False, False, 0)
        content.pack_start(scrolled, True, True, 0)
        
        # Function to load logs
        def load_logs():
            try:
                lines = int(lines_spin.get_value())
                follow = follow_check.get_active()
                
                cmd = ["journalctl", "-u", service_name, "-n", str(lines)]
                if follow:
                    cmd.append("-f")
                    
                # For follow mode, use Popen to stream output
                if follow:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1
                    )
                    
                    # Clear the buffer
                    log_view.get_buffer().set_text("")
                    
                    # Set up a GLib IO watch to read output as it comes
                    def on_output(source, condition):
                        if condition == GLib.IO_IN:
                            line = process.stdout.readline()
                            if line:
                                buffer = log_view.get_buffer()
                                end_iter = buffer.get_end_iter()
                                buffer.insert(end_iter, line)
                                # Auto-scroll to the end
                                log_view.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0.0, 0.0)
                                return True
                        # End of stream or error
                        return False
                    
                    # Add IO watch
                    GLib.io_add_watch(
                        process.stdout,
                        GLib.IO_IN | GLib.IO_HUP,
                        on_output
                    )
                    
                    # Store the process to kill it when the dialog closes
                    dialog.process = process
                    
                else:
                    # For non-follow mode, just run the command and get all output at once
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    log_view.get_buffer().set_text(result.stdout)
                    
            except Exception as e:
                self.show_error_dialog(f"Failed to load logs: {str(e)}")
        
        # Connect signals
        refresh_btn.connect("clicked", lambda w: load_logs())
        follow_check.connect("toggled", lambda w: load_logs() if w.get_active() else None)
        
        # Load logs initially
        load_logs()
        
        # Handle dialog close
        def on_dialog_response(dialog, response_id):
            # Kill the process if it exists
            if hasattr(dialog, 'process') and dialog.process:
                dialog.process.terminate()
            dialog.destroy()
        
        dialog.connect("response", on_dialog_response)
        
        dialog.show_all()

    def create_log_text_view(self):
        """Create a text view for displaying logs"""
        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        
        # Set colors for dark theme
        text_view.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 1))
        text_view.override_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(1, 1, 1, 1))
        
        return text_view

    def get_status_color(self, status):
        """Get color for service status"""
        if status in ["active", "enabled"]:
            return "#27ae60"  # Green
        elif status in ["inactive", "disabled"]:
            return "#7f8c8d"  # Gray
        elif status in ["failed"]:
            return "#c0392b"  # Red
        else:
            return "#2980b9"  # Blue

    def show_edit_host_dialog(self, button):
        """Show dialog to edit an existing remote host"""
        # Get selected host
        selection = self.hosts_list.get_selected_row()
        if not selection:
            self.show_error_dialog("Please select a host to edit")
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        host = self.remote_hosts.get(host_name)
        
        if not host:
            self.show_error_dialog("Host not found")
            return
        
        # Create dialog
        dialog = Gtk.Dialog(
            title="Edit Remote Host",
            parent=self,
            flags=0
        )
        
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content = dialog.get_content_area()
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(6)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        
        # Input fields with existing values
        name_entry = Gtk.Entry()
        name_entry.set_text(host.name)
        
        host_entry = Gtk.Entry()
        host_entry.set_text(host.hostname)
        
        username_entry = Gtk.Entry()
        username_entry.set_text(host.username)
        
        password_entry = Gtk.Entry()
        password_entry.set_visibility(False)  # Hide password characters
        
        # Try to get saved password
        if host.auth_type == "password":
            saved_password = keyring.get_password(
                "systemd-manager",
                f"{host.username}@{host.hostname}"
            )
            if saved_password:
                password_entry.set_text(saved_password)
        
        auth_combo = Gtk.ComboBoxText()
        auth_combo.append_text("Password")
        auth_combo.append_text("SSH Key")
        auth_combo.set_active(0 if host.auth_type == "password" else 1)
        
        key_chooser = Gtk.FileChooserButton(title="Select SSH Key")
        if host.key_path:
            key_chooser.set_filename(host.key_path)
        
        # Make password/key fields sensitive based on auth type
        password_entry.set_sensitive(host.auth_type == "password")
        key_chooser.set_sensitive(host.auth_type == "key")
        
        def on_auth_changed(combo):
            is_password = combo.get_active_text() == "Password"
            password_entry.set_sensitive(is_password)
            key_chooser.set_sensitive(not is_password)
        
        auth_combo.connect("changed", on_auth_changed)
        
        # Layout
        labels = ["Name:", "Hostname:", "Username:", "Password:", "Auth Type:", "SSH Key:"]
        widgets = [name_entry, host_entry, username_entry, password_entry, auth_combo, key_chooser]
        
        for i, (label, widget) in enumerate(zip(labels, widgets)):
            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
        
        content.add(grid)
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            # Remove old host
            if host.name in self.remote_hosts:
                del self.remote_hosts[host.name]
            
            # Create updated host
            updated_host = RemoteHost(
                name=name_entry.get_text(),
                hostname=host_entry.get_text(),
                username=username_entry.get_text(),
                auth_type="key" if auth_combo.get_active_text() == "SSH Key" else "password",
                key_path=key_chooser.get_filename() if auth_combo.get_active_text() == "SSH Key" else None
            )
            
            # Store password in keyring if using password auth
            if updated_host.auth_type == "password":
                keyring.set_password(
                    "systemd-manager",
                    f"{updated_host.username}@{updated_host.hostname}",
                    password_entry.get_text()
                )
            
            # Add updated host
            self.remote_hosts[updated_host.name] = updated_host
            
            # Save changes
            self.save_hosts()
            
            # Refresh the hosts list
            self.refresh_hosts_list()
            
            # If host was connected, disconnect it
            if host_name in self.active_connections:
                client = self.active_connections.get(host_name)
                if client:
                    try:
                        client.close()
                        del self.active_connections[host_name]
                    except:
                        pass
        
        dialog.destroy()

    def on_host_button_press(self, listbox, event):
        """Handle right-click on a host in the hosts list"""
        if event.button == 3:  # Right mouse button
            # Get the row at the clicked position
            row = listbox.get_row_at_y(int(event.y))
            if not row:
                return False
            
            # Get the host name from the row
            host_name = row.get_children()[0].get_children()[1].get_text()
            
            # Create popup menu
            menu = Gtk.Menu()
            
            # Add menu items
            connect_item = Gtk.MenuItem(label="Connect")
            disconnect_item = Gtk.MenuItem(label="Disconnect")
            edit_item = Gtk.MenuItem(label="Edit")
            delete_item = Gtk.MenuItem(label="Delete")
            
            # Set sensitivity based on connection state
            is_connected = host_name in self.active_connections
            connect_item.set_sensitive(not is_connected)
            disconnect_item.set_sensitive(is_connected)
            
            # Connect signals
            connect_item.connect("activate", lambda w: self.on_connect_clicked(None))
            disconnect_item.connect("activate", lambda w: self.on_disconnect_clicked(None))
            edit_item.connect("activate", lambda w: self.show_edit_host_dialog(None))
            delete_item.connect("activate", lambda w: self.on_delete_host_clicked(None))
            
            # Add items to menu
            menu.append(connect_item)
            menu.append(disconnect_item)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(edit_item)
            menu.append(delete_item)
            
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        
        return False

    def format_service_cell(self, column, cell, model, iter, data):
        """Format service name and description in a single cell for remote services"""
        try:
            name = model[iter][0].replace(".service", "")
            description = model[iter][3]  # Description is in column 3 for remote services
            
            # Escape special characters for markup
            if description:
                description = GLib.markup_escape_text(description)
                markup = f'<b>{name}</b>\n<span size="smaller" style="italic">{description}</span>'
            else:
                markup = f'<b>{name}</b>'
                
            cell.set_property("markup", markup)
            
        except Exception as e:
            self.logger.error(f"Error formatting service cell: {str(e)}")
            cell.set_property("text", model.get_value(iter, 0))

    def on_service_activated(self, treeview, path, column):
        """Handle double-click on a remote service"""
        model = treeview.get_model()
        iter = model.get_iter(path)
        service_name = model[iter][0]
        host_name = model[iter][2]
        
        # Show service details dialog
        self.show_remote_service_details(service_name, host_name)

    def show_remote_service_details(self, service_name, host_name):
        """Show detailed information for a remote service"""
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        dialog = Gtk.Dialog(
            title=f"Service Details - {service_name.replace('.service', '')}",
            parent=self,
            flags=0
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(600, 400)
        
        content = dialog.get_content_area()
        
        # Add status header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(12)
        header_box.set_margin_bottom(12)
        
        try:
            # Get enabled status
            stdin, stdout, stderr = client.exec_command(f"systemctl is-enabled {service_name}")
            enabled_status = stdout.read().decode().strip()
            
            # Get active status
            stdin, stdout, stderr = client.exec_command(f"systemctl is-active {service_name}")
            active_status = stdout.read().decode().strip()
            
            # Create status labels with colored backgrounds
            enabled_label = Gtk.Label()
            enabled_label.set_markup(
                f'<span background="{self.get_status_color(enabled_status)}" foreground="white">'
                f' {enabled_status.upper()} </span>'
            )
            
            active_label = Gtk.Label()
            active_label.set_markup(
                f'<span background="{self.get_status_color(active_status)}" foreground="white">'
                f' {active_status.upper()} </span>'
            )
            
            header_box.pack_start(Gtk.Label(label="Status:"), False, False, 0)
            header_box.pack_start(active_label, False, False, 0)
            header_box.pack_start(Gtk.Label(label="Boot Status:"), False, False, 0)
            header_box.pack_start(enabled_label, False, False, 0)
            
        except Exception as e:
            self.logger.error(f"Failed to get service status: {str(e)}")
        
        content.pack_start(header_box, False, False, 0)
        
        # Add notebook with details
        notebook = Gtk.Notebook()
        
        # Status page
        status_view = self.create_log_text_view()
        status_scroll = Gtk.ScrolledWindow()
        status_scroll.add(status_view)
        notebook.append_page(status_scroll, Gtk.Label(label="Status"))
        
        # Properties page
        props_view = self.create_log_text_view()
        props_scroll = Gtk.ScrolledWindow()
        props_scroll.add(props_view)
        notebook.append_page(props_scroll, Gtk.Label(label="Properties"))
        
        # Unit file page
        unit_view = self.create_log_text_view()
        unit_scroll = Gtk.ScrolledWindow()
        unit_scroll.add(unit_view)
        notebook.append_page(unit_scroll, Gtk.Label(label="Unit File"))
        
        content.pack_start(notebook, True, True, 0)
        
        try:
            # Get status
            stdin, stdout, stderr = client.exec_command(f"systemctl status {service_name}")
            status_view.get_buffer().set_text(stdout.read().decode())
            
            # Get properties
            stdin, stdout, stderr = client.exec_command(f"systemctl show {service_name}")
            props_view.get_buffer().set_text(stdout.read().decode())
            
            # Get unit file content
            stdin, stdout, stderr = client.exec_command(f"cat /etc/systemd/system/{service_name} 2>/dev/null || cat /usr/lib/systemd/system/{service_name} 2>/dev/null")
            unit_view.get_buffer().set_text(stdout.read().decode())
            
        except Exception as e:
            self.logger.error(f"Failed to get service details: {str(e)}")
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_service_button_press(self, treeview, event):
        """Handle right-click on a remote service"""
        if event.button == 3:  # Right mouse button
            # Get the service at the clicked position
            path_info = treeview.get_path_at_pos(int(event.x), int(event.y))
            if not path_info:
                return False
                
            path, column, cell_x, cell_y = path_info
            model = treeview.get_model()
            iter = model.get_iter(path)
            service_name = model[iter][0]
            host_name = model[iter][2]
            
            # Check if we're connected to the host
            client = self.active_connections.get(host_name)
            if not client:
                return False
            
            # Create popup menu
            menu = Gtk.Menu()
            
            # Add menu items
            start_item = Gtk.MenuItem(label="Start")
            stop_item = Gtk.MenuItem(label="Stop")
            restart_item = Gtk.MenuItem(label="Restart")
            enable_item = Gtk.MenuItem(label="Enable")
            disable_item = Gtk.MenuItem(label="Disable")
            logs_item = Gtk.MenuItem(label="View Logs")
            
            # Connect signals
            start_item.connect("activate", lambda w: self.on_start_service(service_name, host_name))
            stop_item.connect("activate", lambda w: self.on_stop_service(service_name, host_name))
            restart_item.connect("activate", lambda w: self.on_restart_service(service_name, host_name))
            enable_item.connect("activate", lambda w: self.on_enable_service(service_name, host_name))
            disable_item.connect("activate", lambda w: self.on_disable_service(service_name, host_name))
            logs_item.connect("activate", lambda w: self.show_remote_logs_dialog(service_name, host_name))
            
            # Add items to menu
            menu.append(start_item)
            menu.append(stop_item)
            menu.append(restart_item)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(enable_item)
            menu.append(disable_item)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(logs_item)
            
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        
        return False

    def on_start_service(self, button=None, service_name=None, host_name=None):
        """Start a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to start")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        try:
            # Get sudo password
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl start {service_name}"
            )
            
            if not success:
                return
                
            # Execute command
            cmd = f"echo '{sudo_password}' | sudo -S systemctl start {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to start service: {error}")
            else:
                self.show_info_dialog(f"Service {service_name} started")
                # Refresh the service list
                GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                
        except Exception as e:
            self.show_error_dialog(f"Failed to start service: {str(e)}")

    def on_stop_service(self, button=None, service_name=None, host_name=None):
        """Stop a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to stop")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        try:
            # Get sudo password
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl stop {service_name}"
            )
            
            if not success:
                return
                
            # Execute command
            cmd = f"echo '{sudo_password}' | sudo -S systemctl stop {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to stop service: {error}")
            else:
                self.show_info_dialog(f"Service {service_name} stopped")
                # Refresh the service list
                GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                
        except Exception as e:
            self.show_error_dialog(f"Failed to stop service: {str(e)}")

    def on_restart_service(self, button=None, service_name=None, host_name=None):
        """Restart a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to restart")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        try:
            # Get sudo password
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl restart {service_name}"
            )
            
            if not success:
                return
                
            # Execute command
            cmd = f"echo '{sudo_password}' | sudo -S systemctl restart {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to restart service: {error}")
            else:
                self.show_info_dialog(f"Service {service_name} restarted")
                # Refresh the service list
                GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                
        except Exception as e:
            self.show_error_dialog(f"Failed to restart service: {str(e)}")

    def on_enable_service(self, button=None, service_name=None, host_name=None):
        """Enable a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to enable")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        try:
            # Get sudo password
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl enable {service_name}"
            )
            
            if not success:
                return
                
            # Execute command
            cmd = f"echo '{sudo_password}' | sudo -S systemctl enable {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to enable service: {error}")
            else:
                self.show_info_dialog(f"Service {service_name} enabled")
                # Refresh the service list
                GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                
        except Exception as e:
            self.show_error_dialog(f"Failed to enable service: {str(e)}")

    def on_disable_service(self, button=None, service_name=None, host_name=None):
        """Disable a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to disable")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        try:
            # Get sudo password
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl disable {service_name}"
            )
            
            if not success:
                return
                
            # Execute command
            cmd = f"echo '{sudo_password}' | sudo -S systemctl disable {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to disable service: {error}")
            else:
                self.show_info_dialog(f"Service {service_name} disabled")
                # Refresh the service list
                GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                
        except Exception as e:
            self.show_error_dialog(f"Failed to disable service: {str(e)}")

    def show_remote_logs_dialog(self, service_name=None, host_name=None):
        """Show logs for a remote service"""
        if not service_name or not host_name:
            # Get selected service from treeview
            selection = self.remote_service_view.get_selection()
            model, iter = selection.get_selected()
            if not iter:
                self.show_error_dialog("Please select a service to view logs")
                return
            service_name = model[iter][0]
            host_name = model[iter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog(f"Not connected to {host_name}")
            return
        
        # Create dialog
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name.replace('.service', '')} on {host_name}",
            parent=self,
            flags=0
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(800, 500)
        
        content = dialog.get_content_area()
        
        # Create log view
        log_view = self.create_log_text_view()
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(log_view)
        
        # Add controls
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        lines_label = Gtk.Label(label="Lines:")
        lines_spin = Gtk.SpinButton.new_with_range(10, 1000, 10)
        lines_spin.set_value(100)
        
        refresh_btn = Gtk.Button(label="Refresh")
        
        control_box.pack_start(lines_label, False, False, 0)
        control_box.pack_start(lines_spin, False, False, 0)
        control_box.pack_start(refresh_btn, False, False, 0)
        
        content.pack_start(control_box, False, False, 0)
        content.pack_start(scrolled, True, True, 0)
        
        # Function to load logs
        def load_logs():
            try:
                lines = int(lines_spin.get_value())
                
                # Execute command
                cmd = f"journalctl -u {service_name} -n {lines}"
                stdin, stdout, stderr = client.exec_command(cmd)
                output = stdout.read().decode()
                error = stderr.read().decode()
                
                if error:
                    log_view.get_buffer().set_text(f"Error: {error}")
                else:
                    log_view.get_buffer().set_text(output)
                    
            except Exception as e:
                self.show_error_dialog(f"Failed to load logs: {str(e)}")
        
        # Connect signals
        refresh_btn.connect("clicked", lambda w: load_logs())
        
        # Load logs initially
        load_logs()
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def update_statusbar(self, widget=None, param=None):
        """Update statusbar with system information in a clearer format"""
        current_page = self.stack.get_visible_child_name()
        
        # Clear previous status
        self.statusbar.remove_all(self.statusbar_context)
        
        if current_page == "local":
            try:
                # Get systemd version
                version_cmd = ["systemctl", "--version"]
                version_result = subprocess.run(version_cmd, capture_output=True, text=True)
                version_line = version_result.stdout.split('\n')[0]
                systemd_version = version_line.split(' ')[1]
                
                # Get unit counts - parse the actual count from the output footer
                total_cmd = ["systemctl", "list-unit-files", "--type=service", "--no-pager"]
                total_result = subprocess.run(total_cmd, capture_output=True, text=True)
                total_lines = total_result.stdout.strip().split('\n')
                
                # Extract the actual count from the last line which says "498 unit files listed."
                last_line = total_lines[-1].strip()
                total_units_match = re.search(r'(\d+) unit files listed', last_line)
                total_units = int(total_units_match.group(1)) if total_units_match else len(total_lines) - 2
                
                # Log the raw output for debugging
                self.logger.debug(f"Total services raw output: {len(total_lines)} lines")
                self.logger.debug(f"First line: {total_lines[0]}")
                self.logger.debug(f"Last line: {total_lines[-1]}")
                self.logger.debug(f"Extracted total units: {total_units}")
                
                # Get loaded units count
                loaded_cmd = ["systemctl", "list-units", "--type=service", "--no-pager"]
                loaded_result = subprocess.run(loaded_cmd, capture_output=True, text=True)
                loaded_lines = loaded_result.stdout.strip().split('\n')
                
                # Extract the actual count from the footer line
                loaded_footer = [line for line in loaded_lines if "loaded units listed" in line]
                if loaded_footer:
                    loaded_match = re.search(r'(\d+) loaded units listed', loaded_footer[0])
                    loaded_units = int(loaded_match.group(1)) if loaded_match else len(loaded_lines) - 2
                else:
                    loaded_units = len(loaded_lines) - 2
                
                # Get active units count
                active_cmd = ["systemctl", "list-units", "--type=service", "--state=active", "--no-pager"]
                active_result = subprocess.run(active_cmd, capture_output=True, text=True)
                active_lines = active_result.stdout.strip().split('\n')
                
                # Extract the actual count from the footer line
                active_footer = [line for line in active_lines if "loaded units listed" in line]
                if active_footer:
                    active_match = re.search(r'(\d+) loaded units listed', active_footer[0])
                    active_units = int(active_match.group(1)) if active_match else len(active_lines) - 2
                else:
                    active_units = len(active_lines) - 2
                
                # Calculate unloaded units
                unloaded_units = total_units - loaded_units
                
                # Format the status text with clear sections
                status_text = f"Local System | systemd {systemd_version} | "
                status_text += f"Services: {active_units} active / {loaded_units} loaded / {unloaded_units} unloaded / {total_units} total"
                
                self.statusbar.push(self.statusbar_context, status_text)
                
            except Exception as e:
                self.logger.error(f"Failed to update statusbar: {str(e)}")
                self.statusbar.push(self.statusbar_context, "Local System | Status information unavailable")
        
        elif current_page == "remote":
            # Get selected host
            selection = self.hosts_list.get_selected_row()
            if selection:
                host_name = selection.get_children()[0].get_children()[1].get_text()
                client = self.active_connections.get(host_name)
                
                if client:
                    try:
                        # Get systemd version
                        stdin, stdout, stderr = client.exec_command("systemctl --version")
                        version_line = stdout.readline().strip()
                        systemd_version = version_line.split(' ')[1]
                        
                        # Get unit counts
                        stdin, stdout, stderr = client.exec_command("systemctl list-unit-files --type=service")
                        total_output = stdout.read().decode().strip().split('\n')
                        total_units = len(total_output) - 2  # Subtract header and footer lines
                        
                        stdin, stdout, stderr = client.exec_command("systemctl list-units --type=service | wc -l")
                        loaded_units = int(stdout.readline().strip()) - 1  # Subtract header line
                        
                        stdin, stdout, stderr = client.exec_command("systemctl list-units --type=service --state=active | wc -l")
                        active_units = int(stdout.readline().strip()) - 1  # Subtract header line
                        
                        # Calculate unloaded units
                        unloaded_units = total_units - loaded_units
                        
                        # Format the status text with clear sections
                        status_text = f"Remote Host: {host_name} | systemd {systemd_version} | "
                        status_text += f"Services: {active_units} active / {loaded_units} loaded / {unloaded_units} unloaded / {total_units} total"
                        
                        self.statusbar.push(self.statusbar_context, status_text)
                        return
                    except Exception as e:
                        self.logger.error(f"Failed to update remote statusbar: {str(e)}")
                        self.statusbar.push(self.statusbar_context, f"Remote Host: {host_name} | Connected | Status information unavailable")
                        return
                
                # If not connected
                self.statusbar.push(self.statusbar_context, f"Remote Host: {host_name} | Not connected")
            else:
                self.statusbar.push(self.statusbar_context, "Remote Mode | No host selected")

def main():
    win = SystemdManagerWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
