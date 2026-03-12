#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时测试脚本：获取飞书 Wiki 知识库列表
用完即删。
"""
import os
import sys
import json
import requests

# 从环境变量或 .env 读取
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# 尝试从 .env 文件加载
if not APP_ID or not APP_SECRET:
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "FEISHU_APP_ID":
                    APP_ID = v
                elif k == "FEISHU_APP_SECRET":
                    APP_SECRET = v

if not APP_ID or not APP_SECRET:
    print("❌ 请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量")
    sys.exit(1)

API_BASE = "https://open.feishu.cn/open-apis"


def get_tenant_token():
    resp = requests.post(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"❌ 获取 token 失败: {data}")
        sys.exit(1)
    return data["tenant_access_token"]


def list_wiki_spaces(token):
    """列出应用可访问的所有知识库"""
    headers = {"Authorization": f"Bearer {token}"}
    page_token = ""
    spaces = []

    while True:
        params = {"page_size": "50"}
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"{API_BASE}/wiki/v2/spaces",
            headers=headers,
            params=params,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"❌ 获取知识库列表失败: code={data.get('code')}, msg={data.get('msg')}")
            print(f"   完整响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
            return spaces

        items = (data.get("data") or {}).get("items") or []
        spaces.extend(items)
        page_token = (data.get("data") or {}).get("page_token") or ""
        has_more = (data.get("data") or {}).get("has_more", False)
        if not has_more or not page_token:
            break

    return spaces


def list_space_nodes(token, space_id, parent_node_token=""):
    """列出知识库下的节点（文档列表）"""
    headers = {"Authorization": f"Bearer {token}"}
    page_token = ""
    nodes = []

    while True:
        params = {"page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        if parent_node_token:
            params["parent_node_token"] = parent_node_token

        resp = requests.get(
            f"{API_BASE}/wiki/v2/spaces/{space_id}/nodes",
            headers=headers,
            params=params,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"   ❌ 获取节点失败: code={data.get('code')}, msg={data.get('msg')}")
            return nodes

        items = (data.get("data") or {}).get("items") or []
        nodes.extend(items)
        page_token = (data.get("data") or {}).get("page_token") or ""
        has_more = (data.get("data") or {}).get("has_more", False)
        if not has_more or not page_token:
            break

    return nodes


if __name__ == "__main__":
    print("🔑 获取 tenant_access_token ...")
    token = get_tenant_token()
    print(f"✅ token 获取成功\n")

    print("📚 获取知识库列表 ...")
    spaces = list_wiki_spaces(token)

    if not spaces:
        print("⚠️  没有找到任何可访问的知识库")
        print("   可能原因：")
        print("   1. 应用未开通 wiki:wiki:readonly 权限")
        print("   2. 没有知识库将应用添加为成员")
        print("   3. 企业没有创建任何知识库")
        sys.exit(0)

    print(f"\n找到 {len(spaces)} 个知识库:\n")
    print("-" * 80)

    for i, space in enumerate(spaces, 1):
        space_id = space.get("space_id", "")
        name = space.get("name", "(无名称)")
        desc = space.get("description", "")
        visibility = space.get("visibility", "")
        space_type = space.get("space_type", "")

        print(f"  [{i}] 📖 {name}")
        print(f"      space_id:   {space_id}")
        if desc:
            print(f"      描述:       {desc}")
        print(f"      可见性:     {visibility}")
        print(f"      类型:       {space_type}")

        # 列出顶级节点
        nodes = list_space_nodes(token, space_id)
        if nodes:
            print(f"      顶级节点 ({len(nodes)} 个):")
            for node in nodes[:10]:  # 最多显示10个
                node_token = node.get("node_token", "")
                title = node.get("title", "(无标题)")
                obj_type = node.get("obj_type", "")
                has_child = node.get("has_child", False)
                icon = "📁" if has_child else "📄"
                print(f"        {icon} {title}  (token={node_token}, type={obj_type})")
            if len(nodes) > 10:
                print(f"        ... 还有 {len(nodes) - 10} 个节点")
        else:
            print(f"      (空知识库)")

        print("-" * 80)

    print("\n✅ 完成！")
    print("\n💡 提示: 如需在某个知识库下创建文档，使用 space_id 调用:")
    print("   POST /open-apis/wiki/v2/spaces/{space_id}/nodes")
