import os

import asyncio
import aiofiles
from PIL import Image

#创建临时缓存文件夹
TEMP_DIR = os.path.join(os.path.dirname(__file__), "tmp")
os.makedirs(TEMP_DIR,exist_ok=True)

#获取webp格式的图片缓存并转换为jpg
async def get_img_changeFormat(url, TEMP_DIR, output_format="jpeg"):
    filepath = os.path.join(TEMP_DIR,f"main_{os.path.basename(url)}")
    #获取webp图片文件
    try:
        try:
            import aiohttp as _aiohttp
        except Exception:
            _aiohttp = None
        if _aiohttp is not None:
            async with _aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, timeout=15) as response:
                    if response.status == 200:
                        async with aiofiles.open(filepath,"wb") as f:
                            await f.write(await response.read())
                    else:
                        raise Exception(f"获取封面图失败，状态码: {response.status}")
        else:
            import urllib.request

            def _fetch() -> bytes:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if getattr(resp, "status", 200) != 200:
                        raise Exception("获取封面图失败")
                    return resp.read()

            data = await asyncio.to_thread(_fetch)
            async with aiofiles.open(filepath,"wb") as f:
                await f.write(data)

                #转换为jpg文件
                try:
                    with Image.open(filepath) as img:
                        if output_format =="jpeg":
                            img = img.convert("RGB")
                        output_path = os.path.join(TEMP_DIR,f"change_{os.path.splitext(os.path.basename(url))[0]}.jpg")
                        img.save(output_path, format=output_format.upper())

                except Exception as e:
                    raise Exception(f"转换封面文件失败，错误原因:{Exception(e)}")
                except Exception:
                    raise

                finally:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)

                        except Exception as e:
                            raise Exception(f"删除转换前缓存文件失败，错误原因:{e}")
                        except Exception:
                            raise
    except Exception as e:
        raise Exception(f"获取封面图失败，原因：{e}")
    except Exception:
        raise

    return output_path



