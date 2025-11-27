@echo off
set "VENV_DIR=.venv"
set "REQ_FILE=requirements.txt"

if not exist "%VENV_DIR%" (
    echo [INFO] 未检测到虚拟环境，正在创建...
    python -m venv %VENV_DIR%
    
    echo [INFO] 正在激活...
    call "%VENV_DIR%\Scripts\activate.bat"

    if exist "%REQ_FILE%" (
        echo [INFO] 发现 %REQ_FILE%，正在安装依赖...
        pip install -r %REQ_FILE%
    ) else (
        echo [WARN] 未找到 %REQ_FILE%，跳过安装。
    )
) else (
    echo [INFO] 检测到虚拟环境，直接激活。
    call "%VENV_DIR%\Scripts\activate.bat"
)
git -c user.name="Auto" -c user.email="auto@me.com" commit -a -m "auto commit"
git pull --no-edit
python eagle_chicken_stratge.py config/lighter_bnb.yaml
:: 保持环境激活状态
cmd /k