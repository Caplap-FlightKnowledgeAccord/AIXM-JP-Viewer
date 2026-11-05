@echo off
chcp 65001 > nul
echo === AIXMからDB構築パイプライン (仕様書 2.1, 3.1-3.4) ===
echo.

echo [1/3] Pass 0: ストリーミング分割中...
echo [2/3] Pass 1 & Pass 2: アーカイブDB構築中（全履歴保持）...
echo [3/3] 実運用DB構築中（現在・将来有効データのみ）...
python -c "from src.database.aip_db_builder import build_aip_database; build_aip_database()"
if %errorlevel% neq 0 (
    echo ❌ DB構築に失敗しました
    pause
    exit /b 1
)
echo.
echo ✅ DB構築完了
echo.
echo アーカイブDB: data\processed\duckdb\aip_archive.duckdb (全履歴)
echo 実運用DB:   data\processed\duckdb\aip.duckdb (現在・未来のみ)
echo.
pause