import platform
import sys
import traceback
import subprocess
import atexit
import concurrent
import os
import posixpath
import queue
import socket
import sqlite3
import shutil
import time
import threading
import functools
import plistlib
from pathlib import Path
from threading import Timer
from http.server import HTTPServer, SimpleHTTPRequestHandler

import asyncio
import click
import requests
from packaging.version import parse as parse_version
from pymobiledevice3.cli.cli_common import Command
from pymobiledevice3.exceptions import NoDeviceConnectedError, PyMobileDevice3Exception, DeviceNotFoundError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.tunneld.api import async_get_tunneld_devices
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def start_http_server():
    handler = functools.partial(SimpleHTTPRequestHandler)
    httpd = HTTPServer(("0.0.0.0", 0), handler)
    info_queue.put((get_lan_ip(), httpd.server_port))
    httpd.serve_forever()

def main_callback(service_provider: LockdownClient, dvt: DvtSecureSocketProxyService):
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    ip, port = info_queue.get()
    click.secho(f"Hosting temporary http server on: http://{ip}:{port}/", fg="bright_black")

    afc = AfcService(lockdown=service_provider)
    pc = ProcessControl(dvt)
    
    # Find bookassetd container UUID
    uuid = open("uuid.txt", "r").read().strip() if Path("uuid.txt").exists() else ""
    if len(uuid) < 10:
        try:
            pc.launch("com.apple.iBooks")
        except Exception as e:
            click.secho(f"Error launching Books app: {e}", fg="red")
            return
        click.secho("Finding bookassetd container UUID...", fg="yellow")
        click.secho("Please open Books app and download a book to continue.", fg="yellow")
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if (posixpath.basename(syslog_entry.filename) != 'bookassetd') or \
                    not "/Documents/BLDownloads/" in syslog_entry.message:
                continue
            uuid = syslog_entry.message.split("/var/containers/Shared/SystemGroup/")[1] \
                    .split("/Documents/BLDownloads")[0]
            click.secho(f"Found bookassetd container UUID: {uuid}", fg="yellow")
            with open("uuid.txt", "w") as f:
                f.write(uuid)
            break
    else:
        click.secho("Saved bookassetd container UUID: " + uuid, fg="green")
    
    
    # Modify BLDatabaseManager.sqlite
    # Copy BLDatabaseManager.sqlite to tmp.BLDatabaseManager.sqlite
    filetooverwritename = os.path.basename(path)
    shutil.copyfile("BLDatabaseManager.sqlite", "tmp.BLDatabaseManager.sqlite")
    blconn = sqlite3.connect("tmp.BLDatabaseManager.sqlite")
    cursor = blconn.cursor()
    cursor.execute(f"""
    UPDATE ZBLDOWNLOADINFO
    SET 
        ZASSETPATH = '{path}.zassetpath',
        ZDOWNLOADID = '../../../../../../{path}',
        ZPLISTPATH = '/var/mobile/Media/Downloads/{filetooverwritename}'
    """)
    blconn.commit()

    # Modify downloads.28.sqlitedb
    # Copy downloads.28.sqlitedb to tmp.downloads.28.sqlitedb
    shutil.copyfile("downloads.28.sqlitedb", "tmp.downloads.28.sqlitedb")
    conn = sqlite3.connect("tmp.downloads.28.sqlitedb")
    cursor = conn.cursor()
    bldb_local_prefix = f"/private/var/containers/Shared/SystemGroup/{uuid}/Documents/BLDatabaseManager/BLDatabaseManager.sqlite"
    cursor.execute(f"""
    UPDATE asset
    SET local_path = CASE
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite'
            THEN '{bldb_local_prefix}'
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite-shm'
            THEN '{bldb_local_prefix}-shm'
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite-wal'
            THEN '{bldb_local_prefix}-wal'
    END
    WHERE local_path LIKE '/private/var/containers/Shared/SystemGroup/%/Documents/BLDatabaseManager/BLDatabaseManager.sqlite%'
    """)
    bldb_server_prefix = f"http://{ip}:{port}/tmp.BLDatabaseManager.sqlite"
    cursor.execute(f"""
    UPDATE asset
    SET url = CASE
        WHEN url LIKE '%/BLDatabaseManager.sqlite'
            THEN '{bldb_server_prefix}'
        WHEN url LIKE '%/BLDatabaseManager.sqlite-shm'
            THEN '{bldb_server_prefix}-shm'
        WHEN url LIKE '%/BLDatabaseManager.sqlite-wal'
            THEN '{bldb_server_prefix}-wal'
    END
    WHERE url LIKE '%/BLDatabaseManager.sqlite%'
    """)
    conn.commit()

    # Kill bookassetd and Books processes to stop them from updating BLDatabaseManager.sqlite
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_bookassetd:
        click.secho(f"Stopping bookassetd pid {pid_bookassetd}...", fg="yellow")
        pc.signal(pid_bookassetd, 19)
    if pid_books:
        click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
        pc.kill(pid_books)
    
    # Upload the file
    click.secho("Uploading " + os.path.basename(overridefile), fg="yellow")
    AfcService(lockdown=service_provider).push(overridefile, "Downloads/" + os.path.basename(path))

    # Upload downloads.28.sqlitedb
    click.secho("Uploading downloads.28.sqlitedb", fg="yellow")
    afc.push("tmp.downloads.28.sqlitedb", "Downloads/downloads.28.sqlitedb")
    afc.push("tmp.downloads.28.sqlitedb-shm", "Downloads/downloads.28.sqlitedb-shm")
    afc.push("tmp.downloads.28.sqlitedb-wal", "Downloads/downloads.28.sqlitedb-wal")
    
    # Kill itunesstored to trigger BLDataBaseManager.sqlite overwrite
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid_itunesstored = next((pid for pid, p in procs.items() if p['ProcessName'] == 'itunesstored'), None)
    if pid_itunesstored:
        click.secho(f"Killing itunesstored pid {pid_itunesstored}...", fg="yellow")
        pc.kill(pid_itunesstored)
    
    # Wait for itunesstored to finish download and raise an error
    click.secho("Waiting for itunesstored to finish download...", fg="yellow")
    for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
        if "Install complete for download: 6936249076851270150 result: Failed" in syslog_entry.message:
            click.secho("download complete!", fg="bright_black")
            break
    
    # Kill bookassetd and Books processes to trigger file overwrite
    pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_bookassetd:
        click.secho(f"Killing bookassetd pid {pid_bookassetd}...", fg="yellow")
        pc.kill(pid_bookassetd)
    if pid_books:
        click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
        pc.kill(pid_books)
    
    # Re-open Books app
    try:
        pc.launch("com.apple.iBooks")
    except Exception as e:
        click.secho(f"Error launching Books app: {e}", fg="red")
        return
    
    click.secho("If this takes more than a minute please try again.", fg="yellow")
    click.secho("Waiting for file overwrite to complete...", fg="yellow")
    success_message = path + ") [Install-Mgr]: Marking download as [finished]"
    for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
        if (posixpath.basename(syslog_entry.filename) == 'bookassetd') and \
                success_message in syslog_entry.message:
            break
    pc.kill(pid_bookassetd)
    click.secho("Overwrite successful! Respringing...", fg="green")
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid = next((pid for pid, p in procs.items() if p['ProcessName'] == 'backboardd'), None)
    pc.kill(pid)
    click.secho("Done!", fg="green")

    blconn.close()
    conn.close()
    
    sys.exit(0)

async def _run_async_rsd_connection(address, port):
    try:
        async def async_connection():
            async with RemoteServiceDiscoveryService((address, port)) as rsd:
                click.secho("connected to tunnel", fg="green")
                loop = asyncio.get_running_loop()
                    
                def run_blocking_callback():
                    with DvtSecureSocketProxyService(rsd) as dvt:
                        main_callback(rsd, dvt)
                    
                await loop.run_in_executor(None, run_blocking_callback)

        click.secho(f"attempt connection", fg="bright_black")
        await async_connection()

        return

    except (ConnectionRefusedError, OSError) as e:
        click.secho(f"tunnel connect failed: {e}", fg="red")
        raise

def exit_func(tunnel_proc):
    tunnel_proc.terminate()

async def create_tunnel(udid):
    command = [
        sys.executable,
        "-m", "pymobiledevice3",
        "lockdown", "start-tunnel",
        "--script-mode",
        "--udid", udid
    ]
    tunnel_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    atexit.register(exit_func, tunnel_process)
    while True:
        output = tunnel_process.stdout.readline()
        if output:
            rsd_val = output.decode().strip()
            break
        if tunnel_process.poll() is not None:
            error = tunnel_process.stderr.readlines()
            if error:
                not_connected = None
                admin_error = None
                for i in range(len(error)):
                    if (error[i].find(b'connected') > -1):
                        not_connected = True
                    if (error[i].find(b'admin') > -1):
                        admin_error = True
                if not_connected:
                    print("It seems like your device isn't connected.", error)
                elif admin_error:
                    print("It seems like you're not running this script as admin, which is required.", error)
                else:
                    print("Error opening a tunnel.", error)
                sys.exit()
            break
    rsd_str = str(rsd_val)
    click.secho("Sucessfully created tunnel: " + rsd_str, fg="green")
    click.secho(f"address: {rsd_str.split(" ")[0]}", fg="bright_black")
    port = int(rsd_str.split(" ")[1])
    click.secho(f"port: {port}", fg="bright_black")
    time.sleep(2)
    return {"address": rsd_str.split(" ")[0], "port": int(rsd_str.split(" ")[1])}

async def connection_context(service_provider):# Create a LockdownClient instance
    try:
        marketing_name = service_provider.get_value(key="MarketingName")
        marketing_name = service_provider.get_value(key="MarketingName")
        device_build = service_provider.get_value(key="BuildVersion")
        device_product_type = service_provider.get_value(key="ProductType")
        device_version = parse_version(service_provider.product_version)
        click.secho(f"Got device: {marketing_name} (iOS {device_version}, Build {device_build})", fg="blue")
        click.secho("Please keep your device unlocked during the process.", fg="blue")
        
        # Validate MobileGestalt file
        if os.path.basename(path) == "com.apple.MobileGestalt.plist":
            mg_contents = plistlib.load(open(overridefile, "rb"))
            cache_extra = mg_contents["CacheExtra"]
            if cache_extra is None:
                click.secho("Error: Invalid com.apple.MobileGestalt.plist file", fg="red")
                return
            cache_build_version = mg_contents["CacheVersion"]
            cache_product_type = cache_extra["0+nc/Udy4WNG8S+Q7a/s1A"] # ThinningProductType
            if cache_build_version != device_build or cache_product_type != device_product_type:
                click.secho("Error: It seems you are using MobileGestalt file for a different device", fg="red")
                click.secho(f"Device Build: {device_build}, MobileGestalt Build: {cache_build_version}", fg="red")
                click.secho(f"Device ProductType: {device_product_type}, MobileGestalt ProductType: {cache_product_type}", fg="red")
                # return
        
        if device_version >= parse_version('17.0'):
            available_address = await create_tunnel(service_provider.udid)
            if available_address:
                await _run_async_rsd_connection(available_address["address"], available_address["port"])
            else:
                raise Exception("An error occurred getting tunnels addresses...")
        else:
            # Use USB Mux
            with DvtSecureSocketProxyService(lockdown=service_provider) as dvt:
                main_callback(service_provider, dvt)
    except OSError:  # no route to host (Intel fix)
        pass
    except DeviceNotFoundError:
        click.secho("Device not found. Make sure it's unlocked.", fg="red")
    except Exception as e:
        raise Exception(f"Connection not established... {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python run.py /path/to/local/file (ex. ./MobileGestalt/com.apple.MobileGestalt.plist) /path/to/file_on_iOS (like /private/var/containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache/Library/Caches/com.apple.MobileGestalt.plist)")
        exit(1)
    
    lockdown = create_using_usbmux()
    overridefile = sys.argv[1]
    path = sys.argv[2]
    info_queue = queue.Queue()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(connection_context(lockdown))
