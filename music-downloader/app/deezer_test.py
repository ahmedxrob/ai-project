import httpx
import asyncio


async def main():

    url = "https://api.deezer.com/search"

    params = {
        "q": "moon stormy",
        "limit": 10
    }

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.get(
            url,
            params=params
        )

        print("STATUS:", response.status_code)

        print(
            response.text[:5000]
        )


asyncio.run(main())
