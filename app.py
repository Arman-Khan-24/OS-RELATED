import os
import sys
import psutil
import time
import threading
from typing import List, Dict

# Cross-platform dependencies
is_windows = (os.name == 'nt')
if is_windows:
    import msvcrt
else:
    import select
    import termios
    import tty

# ANSI Codes for native terminal UI
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_CYAN = "\033[36m"
ANSI_REVERSE = "\033[7m"

HISTORY_CAPACITY = 600

class SystemSnapshot:
    def __init__(self, global_cpu: float, global_mem: float, processes: List[Dict]):
        self.global_cpu = global_cpu
        self.global_mem = global_mem
        self.processes = processes

class DataCollector:
    def collect(self) -> SystemSnapshot:
        global_cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        global_mem = mem.percent
        
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                cpu = proc.info['cpu_percent'] or 0.0
                mem_p = proc.info['memory_percent'] or 0.0
                
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name'][:25], # truncate
                    'cpu': cpu,
                    'mem': mem_p
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
                
        # Sort by CPU descending
        processes.sort(key=lambda p: p['cpu'], reverse=True)
        top_processes = processes[:20] # Bounds the array to top 20 UI limits
        
        return SystemSnapshot(global_cpu, global_mem, top_processes)

class RingBuffer:
    def __init__(self, capacity: int):
        self.capacity = capacity
        # Pre-allocate array bounds to mimic C++ O(1) buffer logic
        self.buffer = [None] * capacity
        self.tail = 0
        self.size = 0
        self.lock = threading.Lock()
        
    def push(self, item):
        with self.lock:
            self.buffer[self.tail] = item
            self.tail = (self.tail + 1) % self.capacity
            if self.size < self.capacity:
                self.size += 1
                
    def get_from_newest(self, age: int):
        with self.lock:
            if self.size == 0 or age >= self.size:
                return None
            newest_idx = (self.tail + self.capacity - 1) % self.capacity
            target_idx = (newest_idx + self.capacity - age) % self.capacity
            return self.buffer[target_idx]

def clear_screen():
    sys.stdout.write("\033[H")

def get_keypress():
    """Returns 'LEFT', 'RIGHT', 'SPACE', 'QUIT', or None cross-platform"""
    if is_windows:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'q', b'Q'): return 'QUIT'
            if ch == b' ': return 'SPACE'
            if ch in (b'\xe0', b'\x00'): 
                ext = msvcrt.getch()
                if ext == b'K': return 'LEFT'
                if ext == b'M': return 'RIGHT'
    else:
        # Check standard input for UNIX
        dr, dw, de = select.select([sys.stdin], [], [], 0)
        if dr:
            ch = sys.stdin.read(1)
            if ch in ('q', 'Q'): return 'QUIT'
            if ch == ' ': return 'SPACE'
            if ch == '\033': # Escape sequence for arrow keys
                ch2 = sys.stdin.read(1)
                ch3 = sys.stdin.read(1)
                if ch2 == '[':
                    if ch3 == 'D': return 'LEFT'
                    if ch3 == 'C': return 'RIGHT'
    return None

def run_app():
    sys.stdout.write("\033[?25l") # Hide native cursor
    
    # Warmup calculations
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter(['cpu_percent']): pass
    
    buffer = RingBuffer(HISTORY_CAPACITY)
    collector = DataCollector()
    
    is_running = True
    
    def engine_loop():
        while is_running:
            t0 = time.time()
            snap = collector.collect()
            buffer.push(snap)
            elapsed = time.time() - t0
            
            # Sub-second precision sleep to lock tick-rate over older CPUs
            sleep_time = max(0, 1.0 - elapsed)
            time.sleep(sleep_time)

    engine_thread = threading.Thread(target=engine_loop)
    engine_thread.daemon = True
    engine_thread.start()
    
    current_time_offset = 0
    
    try:
        while is_running:
            key = get_keypress()
            if key == 'QUIT':
                is_running = False
                break
            elif key == 'LEFT':
                current_time_offset += 1
            elif key == 'RIGHT':
                current_time_offset = max(0, current_time_offset - 1)
            elif key == 'SPACE':
                current_time_offset = 0

            # Safe bound locks
            with buffer.lock:
                if buffer.size > 0:
                    current_time_offset = min(current_time_offset, buffer.size - 1)
            
            snap = buffer.get_from_newest(current_time_offset)
            
            clear_screen()
            if not snap:
                sys.stdout.write(f"{ANSI_YELLOW}Warming up System Status: Measuring initial processor delta intervals...{ANSI_RESET}\n")
                sys.stdout.write("\033[J")
            else:
                header = f"{ANSI_CYAN}{ANSI_BOLD}====== Time-Traveling Task Monitor ======\t{ANSI_RESET}"
                if current_time_offset == 0:
                    status = f"{ANSI_GREEN}[ LIVE VIEW ]                        {ANSI_RESET}"
                else:
                    status = f"{ANSI_YELLOW}{ANSI_BOLD}[ REWIND: -{current_time_offset} SECONDS SECURE ]                {ANSI_RESET}"
                sys.stdout.write(f"{header}{status}\n\n")
                
                cpu_color = ANSI_RED if snap.global_cpu > 80.0 else ANSI_GREEN
                mem_color = ANSI_RED if snap.global_mem > 85.0 else ANSI_GREEN
                
                sys.stdout.write(f"{cpu_color}Global CPU Usage: {snap.global_cpu:6.2f}%{ANSI_RESET}\n")
                sys.stdout.write(f"{mem_color}Global RAM Usage: {snap.global_mem:6.2f}%{ANSI_RESET}\n\n")
                
                sys.stdout.write(f"{ANSI_REVERSE}{'PID':<8} {'NAME':<25} {'CPU %':<11} {'MEM %':<11}{ANSI_RESET}\n")
                
                lines_printed = 0
                for proc in snap.processes:
                    name = proc['name']
                    sys.stdout.write(f"{proc['pid']:<8} {name:<25} {proc['cpu']:<11.2f} {proc['mem']:<11.2f}\n")
                    lines_printed += 1
                    
                for _ in range(20 - lines_printed):
                    sys.stdout.write("\n")
                    
                sys.stdout.write(f"\n{ANSI_CYAN} Controls | [Left Arrow] Rewind | [Right Arrow] Forward | [SPACE] Live | [q] Quit {ANSI_RESET}\n")
                sys.stdout.write("\033[J")
            
            sys.stdout.flush()
            time.sleep(0.1) # Fast 10hz rendering loop for instant arrow feedback!
    finally:
        sys.stdout.write("\033[?25h") 

def main():
    if is_windows:
        os.system('color')
        run_app()
    else:
        # Wrap Linux standard execution with tty/termios mapping to allow
        # the same raw unblocked keystroke processing Windows provides natively
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            run_app()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h")
        sys.exit(0)
