#!/usr/bin/env python3
"""
Helper script to get AWS temporary credentials and generate .env file content.
Supports multiple methods: SSO, AssumeRole, and STS GetSessionToken.

Usage:
    python get_aws_credentials.py --method session-token
    python get_aws_credentials.py --method sso --profile my-sso-profile
    python get_aws_credentials.py --method assume-role --role-arn arn:aws:iam::123456789:role/MyRole
    python get_aws_credentials.py --method session-token --output .env
"""

import argparse
import subprocess
import json
import sys
import os


def get_sso_credentials(profile: str) -> dict:
    """
    Get credentials from AWS SSO login.
    
    Args:
        profile: AWS SSO profile name
        
    Returns:
        Dictionary with credentials
    """
    print(f"Logging in with SSO profile: {profile}")
    
    # Initiate SSO login
    try:
        subprocess.run(["aws", "sso", "login", "--profile", profile], check=True)
    except subprocess.CalledProcessError as e:
        print(f"SSO login failed: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("AWS CLI not found. Please install it first.")
        sys.exit(1)
    
    # Get credentials using export-credentials
    try:
        result = subprocess.run(
            ["aws", "configure", "export-credentials", "--profile", profile, "--format", "env"],
            capture_output=True,
            text=True,
            check=True
        )
        
        credentials = {}
        for line in result.stdout.strip().split('\n'):
            if '=' in line:
                # Handle both 'export VAR=value' and 'VAR=value' formats
                line = line.replace('export ', '')
                key, value = line.split('=', 1)
                credentials[key] = value
        
        return credentials
        
    except subprocess.CalledProcessError as e:
        print(f"Failed to export credentials: {e}")
        print(f"stderr: {e.stderr}")
        sys.exit(1)


def get_assume_role_credentials(
    role_arn: str, 
    session_name: str = "ChatSession",
    duration: int = 3600
) -> dict:
    """
    Get credentials by assuming an IAM role.
    
    Args:
        role_arn: ARN of the role to assume
        session_name: Name for the assumed role session
        duration: Session duration in seconds
        
    Returns:
        Dictionary with credentials
    """
    try:
        import boto3
    except ImportError:
        print("boto3 not installed. Run: pip install boto3")
        sys.exit(1)
    
    print(f"Assuming role: {role_arn}")
    
    try:
        sts = boto3.client('sts')
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=duration
        )
        
        creds = response['Credentials']
        return {
            'AWS_ACCESS_KEY_ID': creds['AccessKeyId'],
            'AWS_SECRET_ACCESS_KEY': creds['SecretAccessKey'],
            'AWS_SESSION_TOKEN': creds['SessionToken']
        }
        
    except Exception as e:
        print(f"Failed to assume role: {e}")
        sys.exit(1)


def get_session_token(duration: int = 3600) -> dict:
    """
    Get session token using current credentials.
    Optionally supports MFA.
    
    Args:
        duration: Session duration in seconds
        
    Returns:
        Dictionary with credentials
    """
    try:
        import boto3
    except ImportError:
        print("boto3 not installed. Run: pip install boto3")
        sys.exit(1)
    
    print("Getting session token...")
    
    mfa_serial = input("Enter MFA device ARN (or press Enter to skip): ").strip()
    
    kwargs = {'DurationSeconds': duration}
    
    if mfa_serial:
        mfa_code = input("Enter MFA code: ").strip()
        if not mfa_code:
            print("MFA code is required when MFA device is specified")
            sys.exit(1)
        kwargs['SerialNumber'] = mfa_serial
        kwargs['TokenCode'] = mfa_code
    
    try:
        sts = boto3.client('sts')
        response = sts.get_session_token(**kwargs)
        
        creds = response['Credentials']
        return {
            'AWS_ACCESS_KEY_ID': creds['AccessKeyId'],
            'AWS_SECRET_ACCESS_KEY': creds['SecretAccessKey'],
            'AWS_SESSION_TOKEN': creds['SessionToken']
        }
        
    except Exception as e:
        print(f"Failed to get session token: {e}")
        sys.exit(1)


def get_current_credentials() -> dict:
    """
    Get current credentials from environment or AWS config.
    
    Returns:
        Dictionary with credentials
    """
    credentials = {}
    
    # Check environment variables first
    if os.environ.get('AWS_ACCESS_KEY_ID'):
        credentials['AWS_ACCESS_KEY_ID'] = os.environ['AWS_ACCESS_KEY_ID']
        credentials['AWS_SECRET_ACCESS_KEY'] = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
        if os.environ.get('AWS_SESSION_TOKEN'):
            credentials['AWS_SESSION_TOKEN'] = os.environ['AWS_SESSION_TOKEN']
        return credentials
    
    # Try to get from boto3
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials()
        if creds:
            frozen_creds = creds.get_frozen_credentials()
            credentials['AWS_ACCESS_KEY_ID'] = frozen_creds.access_key
            credentials['AWS_SECRET_ACCESS_KEY'] = frozen_creds.secret_key
            if frozen_creds.token:
                credentials['AWS_SESSION_TOKEN'] = frozen_creds.token
            return credentials
    except Exception:
        pass
    
    print("No credentials found. Please configure AWS credentials first.")
    sys.exit(1)


def generate_env_content(
    credentials: dict, 
    region: str = "us-east-1",
    model_id: str = "anthropic.claude-opus-4-5-20251101-v1:0"
) -> str:
    """
    Generate .env file content.
    
    Args:
        credentials: Dictionary with AWS credentials
        region: AWS region
        model_id: Default Bedrock model ID
        
    Returns:
        String content for .env file
    """
    session_token_line = ""
    if credentials.get('AWS_SESSION_TOKEN'):
        session_token_line = f"AWS_SESSION_TOKEN={credentials['AWS_SESSION_TOKEN']}"
    else:
        session_token_line = "# AWS_SESSION_TOKEN=  # Not using temporary credentials"
    
    return f"""# ============================================
# AWS Configuration (Auto-generated)
# ============================================
AWS_REGION={region}
AWS_ACCESS_KEY_ID={credentials.get('AWS_ACCESS_KEY_ID', '')}
AWS_SECRET_ACCESS_KEY={credentials.get('AWS_SECRET_ACCESS_KEY', '')}
{session_token_line}

# ============================================
# Bedrock Model Configuration
# ============================================
BEDROCK_MODEL_ID={model_id}
MAX_TOKENS=200000
TEMPERATURE=1
TOP_P=1

# ============================================
# Application Configuration
# ============================================
DATABASE_PATH=data/conversations.db
LOG_LEVEL=INFO
DEBUG_MODE=false
"""


def main():
    parser = argparse.ArgumentParser(
        description="Get AWS temporary credentials for the ChatGPT Bedrock application",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Get session token (with optional MFA)
  python get_aws_credentials.py --method session-token
  
  # Login with SSO and export credentials
  python get_aws_credentials.py --method sso --profile my-sso-profile
  
  # Assume a role
  python get_aws_credentials.py --method assume-role --role-arn arn:aws:iam::123456789:role/MyRole
  
  # Export current credentials
  python get_aws_credentials.py --method current
  
  # Save to .env file
  python get_aws_credentials.py --method session-token --output .env
        """
    )
    
    parser.add_argument(
        "--method",
        choices=["sso", "assume-role", "session-token", "current"],
        default="current",
        help="Method to get credentials (default: current)"
    )
    parser.add_argument(
        "--profile",
        help="AWS SSO profile name (required for sso method)"
    )
    parser.add_argument(
        "--role-arn",
        help="IAM Role ARN (required for assume-role method)"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=3600,
        help="Session duration in seconds (default: 3600)"
    )
    parser.add_argument(
        "--model-id",
        default="anthropic.claude-opus-4-5-20251101-v1:0",
        help="Default Bedrock model ID"
    )
    parser.add_argument(
        "--output",
        help="Output file path (default: print to stdout)"
    )
    
    args = parser.parse_args()
    
    # Get credentials based on method
    if args.method == "sso":
        if not args.profile:
            print("Error: --profile is required for SSO method")
            sys.exit(1)
        credentials = get_sso_credentials(args.profile)
    
    elif args.method == "assume-role":
        if not args.role_arn:
            print("Error: --role-arn is required for assume-role method")
            sys.exit(1)
        credentials = get_assume_role_credentials(args.role_arn, duration=args.duration)
    
    elif args.method == "session-token":
        credentials = get_session_token(duration=args.duration)
    
    else:  # current
        credentials = get_current_credentials()
    
    # Generate .env content
    env_content = generate_env_content(
        credentials, 
        region=args.region,
        model_id=args.model_id
    )
    
    # Output
    if args.output:
        # Backup existing file
        if os.path.exists(args.output):
            backup_path = f"{args.output}.backup"
            os.rename(args.output, backup_path)
            print(f"Existing file backed up to: {backup_path}")
        
        with open(args.output, 'w') as f:
            f.write(env_content)
        print(f"✅ Credentials written to: {args.output}")
        
        # Set restrictive permissions on Unix systems
        try:
            os.chmod(args.output, 0o600)
            print(f"   File permissions set to 600 (owner read/write only)")
        except Exception:
            pass  # Windows doesn't support chmod
    else:
        print("\n" + "=" * 60)
        print("Add the following to your .env file:")
        print("=" * 60 + "\n")
        print(env_content)
        print("=" * 60)
        print("\nTip: Use --output .env to save directly to file")


if __name__ == "__main__":
    main()