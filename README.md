# TDAA

TDAA ejecuta pytest para observar fallos, localizar líneas sospechosas con Ochiai y validar parches generados sobre el AST. Requiere CPython 3.12 o superior y pytest.

## Ejecutar en Windows 11

Desde PowerShell, en la raíz del proyecto, usa un `python` que apunte a la versión de CPython elegida. En VS Code selecciona `.venv\Scripts\python.exe` como intérprete del proyecto.

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m tdaa fix inventory.py --tests tests --project examples/demo_project
```

`fix` usa una copia temporal del proyecto y muestra cada validación, las reversiones y el diff aceptado. Añade `--write` para guardar el parche aceptado en el proyecto original. El código de salida es `0` si la suite pasa, `1` si la búsqueda termina sin parche y `2` si un problema de ejecución o colección impide completar la búsqueda.

## Límites del sandbox

- Cada observación o validación usa un intérprete independiente. `wall` limita la sesión completa, incluida la colección y el arranque. `timeout` limita cada prueba. El supervisor concede hasta 250 ms para que pytest registre un timeout antes de terminar el proceso por la fuerza, siempre sujeto al límite de la sesión. Si lo termina, conserva los resultados de las pruebas que ya acabaron y marca como pendientes las que quedaron sin ejecutar.
- En Windows, un Job Object limita la memoria comprometida del árbol de procesos a `mem_mb` y termina sus miembros al cerrar el último handle. El proceso se incorpora al Job antes de arrancar, incluso si el intérprete seleccionado es el lanzador de un entorno virtual. Si Windows impide aplicar el límite, la ejecución se detiene con un diagnóstico. Pytest registra `MemoryError` cuando el intérprete lanza esa excepción.
- En Unix, el proceso usa `RLIMIT_AS` para el espacio de direcciones y `RLIMIT_CPU` para la CPU. El supervisor termina el grupo de procesos. Los límites POSIX son por proceso y dependen del kernel. Un descendiente que cree una sesión o grupo nuevos puede quedar fuera de la limpieza del grupo original.
- El sandbox proporciona aislamiento de procesos y límites de recursos. El código conserva los permisos del usuario, incluido el acceso a archivos y a la red.
