import asyncio
import logging
import time

import eth_account
import lighter

logging.basicConfig(level=logging.DEBUG)

# this is a dummy private key which is registered on Testnet.
# It serves as a good example
BASE_URL = "https://mainnet.zklighter.elliot.ai"
ETH_PRIVATE_KEY = "0xf3ea4be37b29a5c3bc4fb53260738f62f9c22cba5d3262b21c7b6fcde8fcd178"
API_KEY_INDEX = 8


async def main():
    # verify that the account exists & fetch account index
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    eth_acc = eth_account.Account.from_key(ETH_PRIVATE_KEY)
    eth_address = eth_acc.address

    try:
        response = await lighter.AccountApi(api_client).accounts_by_l1_address(
            l1_address=eth_address
        )
    except lighter.ApiException as e:
        if e.data.message == "account not found":
            print(f"error: account not found for {eth_address}")
            return
        else:
            raise e

    if len(response.sub_accounts) > 1:
        for sub_account in response.sub_accounts:
            print(f"found accountIndex: {sub_account.index}")

        print("multiple accounts found, using the first one")
        account_index = response.sub_accounts[0].index
    else:
        account_index = response.sub_accounts[0].index

    # create a private/public key pair for the new API key
    # pass any string to be used as seed for create_api_key like
    # create_api_key("Hello world random seed to make things more secure")
    private_key, public_key, err = lighter.create_api_key()
    if err is not None:
        raise Exception(err)

    tx_client = lighter.SignerClient(
        url=BASE_URL,
        private_key=private_key,
        account_index=account_index,
        api_key_index=API_KEY_INDEX,
    )

    # change the API key
    response, err = await tx_client.change_api_key(
        eth_private_key=ETH_PRIVATE_KEY,
        new_pubkey=public_key,
    )
    if err is not None:
        raise Exception(err)

    # wait some time so that we receive the new API key in the response
    time.sleep(10)

    # check that the API key changed on the server
    err = tx_client.check_client()
    if err is not None:
        raise Exception(err)

    print(
        f"""
BASE_URL = '{BASE_URL}'
API_KEY_PRIVATE_KEY = '{private_key}'
ACCOUNT_INDEX = {account_index}
API_KEY_INDEX = {API_KEY_INDEX}
    """
    )

    await tx_client.close()
    await api_client.close()


if __name__ == "__main__":
    asyncio.run(main())