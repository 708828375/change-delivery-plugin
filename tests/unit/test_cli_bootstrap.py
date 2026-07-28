import subprocess
import sys


def test_python_module_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "change_delivery", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Change Delivery" in result.stdout
