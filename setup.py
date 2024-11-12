import os
import signal
import subprocess
import sys
import time

from dotenv import load_dotenv


def run_command(command, name=None):
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if name:
            print(f"Started {name} process with PID: {process.pid}")
        return process
    except Exception as e:
        print(f"Error starting {name}: {e}")
        return None


def cleanup_processes(processes):
    for name, process in processes.items():
        if process:
            print(f"Stopping {name}...")
            process.terminate()
            process.wait()
    print("All processes stopped")


def main():
    # Load environment variables
    load_dotenv()

    processes = {}

    try:
        # Start poetry shell
        print("Activating poetry environment...")
        run_command("poetry install")

        # Start bot and proxy processes
        processes["bot"] = run_command("poetry run bot", "Bot")
        time.sleep(2)  # Wait for bot to initialize
        processes["proxy"] = run_command("poetry run proxy", "Proxy")
        time.sleep(2)  # Wait for proxy to initialize

        # Start two ngrok processes on different ports
        processes["ngrok1"] = run_command("ngrok http 8766", "Teacher Ngrok")
        processes["ngrok2"] = run_command("ngrok http 8767", "Student Ngrok")
        time.sleep(5)  # Wait for ngrok to initialize

        # Get ngrok URLs
        ngrok_output = subprocess.check_output(
            ["curl", "http://localhost:4040/api/tunnels"], text=True
        )
        teacher_url = input("Enter the HTTPS URL from the first ngrok terminal: ")
        student_url = input("Enter the HTTPS URL from the second ngrok terminal: ")

        meeting_url = input("Enter the meeting URL (must start with https://): ")

        # Start teacher and student bots
        teacher_cmd = f'poetry run meetingbaas --meeting-url "{meeting_url}" --ngrok-url "{teacher_url}" --bot-name "Teacher" --bot-image "https://utfs.io/f/teacher-image-url"'
        student_cmd = f'poetry run meetingbaas --meeting-url "{meeting_url}" --ngrok-url "{student_url}" --bot-name "Student" --bot-image "https://utfs.io/f/student-image-url"'

        processes["teacher"] = run_command(teacher_cmd, "Teacher Bot")
        processes["student"] = run_command(student_cmd, "Student Bot")

        print("\nAll processes started! Press Ctrl+C to stop everything.")

        # Keep the script running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down...")
        cleanup_processes(processes)
        sys.exit(0)
    except Exception as e:
        print(f"An error occurred: {e}")
        cleanup_processes(processes)
        sys.exit(1)


if __name__ == "__main__":
    main()
