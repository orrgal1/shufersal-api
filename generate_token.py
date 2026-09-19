import argparse
import sys
from security import generate_jwt_token

def main():
    parser = argparse.ArgumentParser(description="Generate a JWT token for Shufersal Local API")
    parser.add_argument("--subject", "-s", default="remote-agent", help="Subject / Agent identifier (default: remote-agent)")
    parser.add_argument("--days", "-d", type=int, default=365, help="Token validity in days (default: 365)")
    args = parser.parse_args()

    token = generate_jwt_token(subject=args.subject, expires_days=args.days)
    print(token)

if __name__ == "__main__":
    main()
