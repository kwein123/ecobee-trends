"""One-time interactive login that bootstraps a token config file.

Prompts for ecobee username/password (and MFA code if the account requires
it), performs the web login, and writes access/refresh tokens to a JSON
config file. The password is NOT stored — only the tokens, which the logger
(and this library) can refresh indefinitely without re-prompting.

Usage:
    python app/ecobee_login.py [--config ./data/auth/ecobee.conf]
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

# Allow running from a plain checkout without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyecobee import (
    Ecobee,
    EcobeeAuthFailedError,
    EcobeeAuthMfaRequiredError,
    EcobeeAuthUnknownError,
)

DEFAULT_CONFIG = os.environ.get("ECOBEE_CONFIG", "./data/auth/ecobee.conf")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to write the token config file (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    config_path = os.path.expanduser(args.config)
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)

    ecobee = Ecobee(config_filename=config_path)
    ecobee.username = input("ecobee username/email: ").strip()
    ecobee.password = getpass.getpass("ecobee password: ")

    try:
        ecobee.request_tokens_web()
    except EcobeeAuthMfaRequiredError as exc:
        challenge = exc.args[0]
        prompt = {
            "otp": "Enter the 6-digit code from your authenticator app",
            "sms": "Enter the code sent to your phone via SMS",
        }.get(challenge.mfa_type, f"Enter the {challenge.mfa_type} MFA code")
        code = input(f"{prompt}: ").strip()
        try:
            ecobee.submit_mfa_code(challenge, code)
        except EcobeeAuthFailedError as err:
            print(f"MFA code rejected: {err}", file=sys.stderr)
            return 2
    except (EcobeeAuthFailedError, EcobeeAuthUnknownError) as err:
        print(f"Login failed: {err}", file=sys.stderr)
        return 2

    if not ecobee.refresh_token:
        print(
            "Login succeeded but no refresh_token was issued; the logger "
            "would need to re-authenticate every run. Aborting.",
            file=sys.stderr,
        )
        return 3

    # The library persists the password alongside the tokens; scrub it so the
    # config file only holds tokens.
    ecobee.password = None
    ecobee._write_config()
    os.chmod(config_path, 0o600)

    if not ecobee.get_thermostats():
        print("Tokens written, but a test thermostat fetch failed.", file=sys.stderr)
        return 4

    names = ", ".join(t.get("name", t.get("identifier", "?")) for t in ecobee.thermostats)
    print(f"Success. Tokens written to {config_path}")
    print(f"Thermostats visible on this account: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
