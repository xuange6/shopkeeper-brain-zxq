import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from minio.error import S3Error
from minio import Minio

# 明确加载 knowledge/.env，避免从不同目录运行时读不到 MinIO 配置
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_minio_client():
    # 1. 从 .env 读取 MinIO 连接配置
    try:
        endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
        access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
        secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
        bucket_name = os.getenv("MINIO_BUCKET_NAME", "").strip()
        secure = _parse_bool(os.getenv("MINIO_SECURE", "false"))

        # MinIO SDK 的 endpoint 只要 host:port，不要 http:// 或 https://
        if endpoint.startswith("http://"):
            endpoint = endpoint[len("http://"):]
            secure = False
        elif endpoint.startswith("https://"):
            endpoint = endpoint[len("https://"):]
            secure = True

        if not endpoint or not access_key or not secret_key or not bucket_name:
            logging.error("MinIO 配置不完整，请检查 MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET_NAME")
            return None

        # 2. 实例化 MinIO 客户端
        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

        # 3. 判断桶是否存在，不存在就创建
        bucket_exists = client.bucket_exists(bucket_name)
        if not bucket_exists:
            client.make_bucket(bucket_name)
            logging.info(f"桶:{bucket_name} 不存在，已创建")
        else:
            logging.info(f"桶:{bucket_name} 已经存在")

        # 4. 返回 MinIO 客户端
        return client
    except S3Error as e:
        logging.error(f"MinIO 客户端创建失败: {e}")
        return None
    except Exception as e:
        logging.error(f"MinIO 客户端初始化失败: {e}")
        return None
