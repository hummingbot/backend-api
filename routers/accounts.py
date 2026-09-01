from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from deps import get_accounts_service, require_gateway_online
from models import GatewayWalletCredential, SetDefaultWalletRequest
from services.accounts_service import AccountsService, validate_safe_name

router = APIRouter(tags=["Accounts"], prefix="/accounts")


@router.get("/", response_model=List[str])
async def list_accounts(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all account names in the system.

    Returns:
        List of account names
    """
    return accounts_service.list_accounts()


@router.get("/{account_name}/credentials", response_model=List[str])
async def list_account_credentials(account_name: str,
                                   accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all connectors that have credentials configured for a specific account.

    Args:
        account_name: Name of the account to list credentials for

    Returns:
        List of connector names that have credentials configured

    Raises:
        HTTPException: 404 if account not found
    """
    try:
        credentials = accounts_service.list_credentials(account_name)
        # Remove .yml extension from filenames
        return [cred.replace('.yml', '') for cred in credentials]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add-account", status_code=status.HTTP_201_CREATED)
async def add_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Create a new account with default configuration files.

    Args:
        account_name: Name of the new account to create

    Returns:
        Success message when account is created

    Raises:
        HTTPException: 400 if account already exists or the account name is invalid
    """
    validate_safe_name(account_name, "account name")
    try:
        accounts_service.add_account(account_name)
        return {"message": "Account added successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete-account")
async def delete_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Delete an account and all its associated credentials.

    Args:
        account_name: Name of the account to delete

    Returns:
        Success message when account is deleted

    Raises:
        HTTPException: 400 if trying to delete master account or the account name is invalid, 404 if account not found
    """
    validate_safe_name(account_name, "account name")
    try:
        if account_name == "master_account":
            raise HTTPException(status_code=400, detail="Cannot delete master account.")
        await accounts_service.delete_account(account_name)
        return {"message": "Account deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/delete-credential/{account_name}/{connector_name}")
async def delete_credential(
    account_name: str,
    connector_name: str,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Delete a specific connector credential for an account.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector to delete credentials for

    Returns:
        Success message when credential is deleted

    Raises:
        HTTPException: 404 if credential not found
    """
    try:
        await accounts_service.delete_credentials(account_name, connector_name)
        return {"message": "Credential deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/add-credential/{account_name}/{connector_name}", status_code=status.HTTP_201_CREATED)
async def add_credential(
    account_name: str,
    connector_name: str,
    credentials: Dict,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Add or update connector credentials (API keys) for a specific account and connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector
        credentials: Dictionary containing the connector credentials

    Returns:
        Success message when credentials are added

    Raises:
        HTTPException: 400 if there's an error adding the credentials
    """
    try:
        await accounts_service.add_credentials(account_name, connector_name, credentials)
        return {"message": "Connector credentials added successfully."}
    except Exception as e:
        # Rollback is handled inside add_credentials, which only deletes the file for a
        # brand-new creation and preserves pre-existing credentials on a failed update.
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Gateway Wallet Management Endpoints
# ============================================

@router.get("/gateway/wallets")
async def list_gateway_wallets(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    List all wallets managed by Gateway.
    Gateway manages its own encrypted wallet storage.

    Returns:
        List of wallet information from Gateway

    Raises:
        HTTPException: 503 if Gateway unavailable
    """
    try:
        wallets = await accounts_service.get_gateway_wallets()
        return wallets
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gateway/add-wallet", status_code=status.HTTP_201_CREATED)
async def add_gateway_wallet(
    wallet_credential: GatewayWalletCredential,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Add an existing wallet to Gateway using its private key.
    Gateway handles encryption and storage internally.

    Args:
        wallet_credential: Wallet credentials (chain, private_key, and optional set_default)

    Returns:
        Wallet information from Gateway including address

    Raises:
        HTTPException: 503 if Gateway unavailable, 400 on validation error
    """
    try:
        result = await accounts_service.add_gateway_wallet(
            chain=wallet_credential.chain,
            private_key=wallet_credential.private_key,
            set_default=wallet_credential.set_default
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gateway/wallet/set-default", dependencies=[Depends(require_gateway_online)])
async def set_default_gateway_wallet(
    request: SetDefaultWalletRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Set the default wallet for a chain in Gateway.

    When multiple wallets are configured for a chain, this endpoint allows
    switching which wallet is used as the default for operations.

    Args:
        request: Contains chain and wallet address to set as default

    Returns:
        Dict with success status and updated wallet info.

    Example: POST /accounts/gateway/wallet/set-default
    {
        "chain": "solana",
        "address": "82SggYRE2Vo4jN4a2pk3aQ4SET4ctafZJGbowmCqyHx5"
    }
    """
    try:
        result = await accounts_service.gateway_client.set_default_wallet(
            chain=request.chain,
            address=request.address
        )

        if result is None:
            raise HTTPException(status_code=502, detail="Failed to set default wallet: Gateway returned no response")

        if "error" in result:
            raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {result.get('error')}")

        return {
            "success": True,
            "message": f"Set {request.address} as default wallet for {request.chain}",
            "chain": request.chain,
            "address": request.address
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting default wallet: {str(e)}")


@router.delete("/gateway/{chain}/{address}")
async def remove_gateway_wallet(
    chain: str,
    address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Remove a wallet from Gateway.

    Args:
        chain: Blockchain chain (e.g., 'solana', 'ethereum')
        address: Wallet address to remove

    Returns:
        Success message

    Raises:
        HTTPException: 503 if Gateway unavailable
    """
    try:
        result = await accounts_service.remove_gateway_wallet(chain, address)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
