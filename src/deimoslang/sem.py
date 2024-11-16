from typing import Optional
from enum import Enum, auto

from .tokenizer import Tokenizer, Token
from .parser import *
from .types import *


class SemError(Exception):
    pass


class Scope:
    def __init__(self, parent: Optional["Scope"], is_block: bool):
        self.parent = parent
        self._syms: list[Symbol] = []
        self._unique_player_selectors: set[PlayerSelector] = set()
        self._active_vars: list[Symbol] = []
        self._cleaned_vars: set[Symbol] = set() # if all branching scopes agree on cleanup, do not clean up the same variables again
        self.is_block = is_block # cleanup must not cross a block boundary as we do not have the notion of a moved variable

    def new_block(self) -> "Scope":
        return Scope(parent=self, is_block=True)

    def new_branch(self) -> "Scope":
        res = Scope(parent=self, is_block=False)

        # when a branch cleans up, the variables may still be active in the parent or other branches
        res._active_vars = self._active_vars[:]

        return res

    # Only blocks require lookup for now. Variables are only used internally and the sym is always locally known
    def lookup_block_by_name(self, literal: str) -> Symbol:
        for sym in reversed(self._syms):
            if sym.kind != SymbolKind.block:
                continue
            if sym.literal == literal:
                return sym
        if self.parent is not None:
            return self.parent.lookup_block_by_name(literal)
        raise SemError(f"Unable to find symbol in scope: {literal}")

    def is_block_local_var(self, sym: Symbol) -> bool:
        cur = self
        while sym is not None:
            if sym in cur._syms:
                return True
            elif cur.is_block:
                # must be checked after cur._syms
                break
            cur = cur.parent
        return False

    def put_sym(self, sym: Symbol) -> Symbol:
        self._syms.append(sym)
        return sym

    def activate_var(self, sym: Symbol):
        if sym in self._active_vars:
            raise SemError(f"Attempted to activate an already active variable: {sym}")
        self._active_vars.append(sym)

    def kill_var(self, sym: Symbol):
        if not self.is_block_local_var(sym):
            raise SemError(f"Attempted to kill a variable that isn't local to the current block")
        if sym not in self._active_vars:
            raise SemError(f"Attempted to kill an inactive variable: {sym}")
        self._cleaned_vars.add(sym)
        self._active_vars.remove(sym)


class Analyzer:
    def __init__(self, stmts: list[Stmt]):
        self.scope = Scope(parent=None, is_block=True)
        self._next_sym_id = 0
        self._block_defs: list[BlockDefStmt] = []
        self._stmts = stmts

        self._block_nesting_level = 0
        self._loop_nesting_level = 0
        self._loop_nesting_stack = []

    def open_block(self):
        self.scope = self.scope.new_block()

        self._loop_nesting_stack.append(self._loop_nesting_level)
        self._loop_nesting_level = 0
        self._block_nesting_level += 1

    def close_block(self):
        self.scope = self.scope.parent
        self._loop_nesting_level = self._loop_nesting_stack.pop()
        self._block_nesting_level -= 1

    def open_loop(self):
        self.scope = self.scope.new_branch()
        self._loop_nesting_level += 1

    def close_loop(self):
        self.scope = self.scope.parent
        self._loop_nesting_level-=1

    def gen_sym_id(self) -> int:
        result = self._next_sym_id
        self._next_sym_id += 1
        return result

    def gen_block_sym(self, name: str) -> Symbol:
        return self.scope.put_sym(Symbol(name, self.gen_sym_id(), SymbolKind.block))

    def gen_var_sym(self, name="anonymous") -> Symbol:
        return self.scope.put_sym(Symbol(f":{name}:", self.gen_sym_id(), SymbolKind.variable))

    def gen_label_sym(self, name="anonymous") -> Symbol:
        return self.scope.put_sym(Symbol(f":{name}", self.gen_sym_id(), SymbolKind.label))

    def def_var(self) -> Symbol:
        var_sym = self.gen_var_sym()
        self.scope.activate_var(var_sym)
        return var_sym

    def mark_var_dead(self, sym: Symbol):
        self.scope.kill_var(sym)

    def gen_cleanup_all_vars(self) -> StmtList:
        res = []
        for var in reversed(self.scope._active_vars):
            self.mark_var_dead(var)
            res.append(KillVarStmt(var))
        return StmtList(res)

    def sem_expr(self, expr: Expression) -> Expression:
        return expr # TODO

    def sem_stmt(self, stmt: Stmt) -> Stmt:
        match stmt:
            case BlockDefStmt():
                if not isinstance(stmt.name, IdentExpression):
                    raise SemError(f"Only IdentExpression is allowed during block declaration")
                sym = self.gen_block_sym(stmt.name.ident)
                stmt.body.stmts.append(ReturnStmt())
                self.open_block()
                stmt.body = self.sem_stmt(stmt.body)
                self.close_block()
                stmt.name = SymExpression(sym)
                sym.defnode = stmt
                self._block_defs.append(stmt)
                return None
            case StmtList():
                res = []
                for inner in stmt.stmts:
                    if semmed := self.sem_stmt(inner):
                        res.append(semmed)
                return StmtList(res)
            case CallStmt():
                if isinstance(stmt.name, IdentExpression):
                    sym = self.scope.lookup_block_by_name(stmt.name.ident)
                elif isinstance(stmt.name, SymExpression):
                    sym = stmt.name.sym
                else:
                    raise SemError(f"Malformed call: {stmt}")
                stmt.name = SymExpression(sym)
                return stmt
            case CommandStmt():
                self.scope._unique_player_selectors.add(stmt.command.player_selector)
                return stmt
            case IfStmt():
                stmt.expr = self.sem_expr(stmt.expr)

                self.scope = self.scope.new_branch()
                stmt.branch_true = self.sem_stmt(stmt.branch_true)
                self.scope = self.scope.parent

                self.scope = self.scope.new_branch()
                stmt.branch_false = self.sem_stmt(stmt.branch_false)
                self.scope = self.scope.parent
                return stmt
            case LoopStmt():
                self.open_loop()
                stmt.body = self.sem_stmt(stmt.body)
                self.close_loop()
                return stmt
            case WhileStmt():
                stmt.expr = self.sem_expr(stmt.expr)
                self.open_loop()
                stmt.body = self.sem_stmt(stmt.body)
                self.close_loop()
                return stmt
            case UntilStmt():
                self.open_loop()
                expr = self.sem_expr(stmt.expr) # sem ahead of time because it's used twice
                body = self.sem_stmt(stmt.body)
                self.close_loop()
                return IfStmt(
                    expr,
                    branch_true=StmtList([]),
                    branch_false=StmtList([
                        UntilRegion(
                            expr=expr,
                            body=WhileStmt(
                                UnaryExpression(Token(TokenKind.keyword_not, "not", LineInfo(-1, -1, -1)), expr),
                                body
                            ),
                        ),
                    ])
                )
            case TimesStmt():
                var_sym = self.def_var()
                prologue = [
                    DefVarStmt(var_sym),
                    WriteVarStmt(var_sym, NumberExpression(stmt.num)),
                ]
                epilogue = [
                    KillVarStmt(var_sym),
                ]
                cond = GreaterExpression(ReadVarExpr(SymExpression(var_sym)), NumberExpression(0))
                stmt.body.stmts.append(
                    WriteVarStmt(var_sym, SubExpression(ReadVarExpr(SymExpression(var_sym)), NumberExpression(1)))
                )

                res = StmtList(prologue + [self.sem_stmt(WhileStmt(cond, stmt.body))] + epilogue)
                self.mark_var_dead(var_sym)
                return res
            case ReturnStmt():
                if self._block_nesting_level <= 0:
                    raise SemError(f"Return used outside of block scope")
                return StmtList([
                    self.gen_cleanup_all_vars(),
                    stmt
                ])
            case BreakStmt():
                if self._loop_nesting_level <= 0:
                    raise SemError(f"Break used outside of loop scope")
                return stmt
            case DefVarStmt() | WriteVarStmt() | KillVarStmt():
                return stmt
            case _:
                raise SemError(f"Unhandled statement type: {stmt}")
        raise SemError(f"Statement fell through: {stmt}")

    def analyze_program(self):
        res = []
        for stmt in self._stmts:
            if semmed := self.sem_stmt(stmt):
                res.append(semmed)
        self._stmts = res


if __name__ == "__main__":
    from pathlib import Path

    toks = Tokenizer().tokenize(Path("./testbot.txt").read_text())
    parser = Parser(toks)
    parsed = (parser.parse())

    analyzer = Analyzer(parsed)
    analyzer.analyze_program()

    for block in analyzer._block_defs:
        print_cmd(str(block))

    for stmt in analyzer._stmts:
        print_cmd(str(stmt))