# Mermaid 兼容性探针（临时文件，验证完会删）

目的是找出 **GitHub 端**能渲染、而本地也一致的写法。

## 1 双向箭头 + 带引号边标签

```mermaid
flowchart LR
    A["甲"] <-->|"标签文字"| B["乙"]
```

## 2 双向箭头 无标签

```mermaid
flowchart LR
    A["甲"] <--> B["乙"]
```

## 3 单向箭头 + 无引号边标签

```mermaid
flowchart LR
    A["甲"] -->|标签文字| B["乙"]
```

## 4 无箭头连线

```mermaid
flowchart LR
    A["甲"] --- B["乙"]
```

## 5 br 换行 + 中文括号

```mermaid
flowchart LR
    A["甲<br/>（括号）"] --> B["乙"]
```

## 6 完整当前图（README 里那段）

```mermaid
flowchart LR
    W["Windows<br/>便笺"] <--> MS["微软便笺云端<br/>（Exchange 笔记）"]
    N["iPhone / iPad / Mac<br/>备忘录"] <-->|"加 hotmail 账号"| MS
    MS <--> SV["sticky-mi-sync<br/>（你的 NAS / 软路由）"]
    SV <--> MI["小米笔记云端"]
    MI <--> M["Android<br/>小米笔记"]
```

## 7 保守版：节点先声明 + 无引号标签 + 少用双向

```mermaid
flowchart LR
    W["Windows 便笺"]
    MS["微软便笺云端"]
    N["iPhone / iPad / Mac 备忘录"]
    SV["sticky-mi-sync 你的 NAS"]
    MI["小米笔记云端"]
    M["Android 小米笔记"]

    W --> MS
    N --> MS
    MS <--> SV
    SV <--> MI
    MI --> M
```

## 8 完全不用双向箭头（最保守）

```mermaid
flowchart LR
    W["Windows 便笺"]
    MS["微软便笺云端"]
    N["iPhone / iPad / Mac 备忘录"]
    SV["sticky-mi-sync 你的 NAS"]
    MI["小米笔记云端"]
    M["Android 小米笔记"]

    W --> MS
    N --> MS
    MS --> SV
    SV --> MI
    MI --> M
```
