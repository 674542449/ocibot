"""Firewall dialog Chinese labels / helpers (no live OCI calls)."""

from app.dialogs import _format_firewall_rule_line


def test_format_firewall_rule_line_prefers_chinese_labels():
    text = _format_firewall_rule_line(
        {
            "direction": "INGRESS",
            "direction_label": "入站",
            "protocol": "6",
            "protocol_label": "TCP",
            "cidr": "0.0.0.0/0",
            "port": "22",
            "stateless": False,
            "description": "SSH",
        }
    )
    assert "【入站】" in text
    assert "TCP" in text
    assert "0.0.0.0/0" in text
    assert "端口 22" in text
    assert "有状态" in text
    assert "SSH" in text
    assert "INGRESS" not in text


def test_format_firewall_rule_line_falls_back_from_raw_codes():
    text = _format_firewall_rule_line(
        {
            "direction": "EGRESS",
            "protocol": "all",
            "cidr": "::/0",
            "port": "全部",
            "stateless": True,
            "description": "",
        }
    )
    assert "【出站】" in text
    assert "全部协议" in text
    assert "无状态" in text
    assert "EGRESS" not in text
