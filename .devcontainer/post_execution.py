#!/usr/bin/env python3

import os
import sys
import time
import typing
import subprocess

def run(string:str, print_out:bool=True) -> typing.Dict[str, typing.Union[str, int]]:
    if print_out:
        print(string)

    output: typing.Dict[str, typing.Union[str, int]] = {
        "stdout": "",
        "stderr": "",
        "returncode": -1
    }

    try:
        result = subprocess.run(string.split(" "), capture_output=True, text=True, check=False)
        output["stdout"] = result.stdout
        output["stderr"] = result.stderr
        output["returncode"] = result.returncode
    except Exception as e:
        output["stderr"] = str(e)

    return output


if __name__ == "__main__":
    commands_to_manually_run:typing.List[str] = []

    for command in [
        "code --install-extension wso2.ballerina@4.7.9"
    ]:
        command_success:bool = False
        for _ in range(10):
            result:typing.Dict[str, typing.Union[str, int]] = run(command)
            if int(result["returncode"]) == 0:
                command_success = True
                break
            time.sleep(5)

        if not command_success:
            commands_to_manually_run.append(command)

    if len(commands_to_manually_run) == 0:
        print("SUCCESS")
    else:
        manual_script:str = "manual_run.sh"

        print(f"The following commands could not be run automatically. Please run the script {manual_script} manually or open a shell")
        with open(manual_script,"w+") as f:
            for command in commands_to_manually_run:
                f.write(command + "\n")
            f.write("rm -- \"$0\"\n")
        run(f"chmod 777 {manual_script}")
        full_manual_script:str = os.path.abspath(manual_script)

        with open(f"{os.path.expanduser('~')}/.bashrc", "a+") as writer:
            writer.write(f"""\nif [ -f "{full_manual_script}" ]; then {full_manual_script}; fi\n\n""")

        sys.exit(1)

