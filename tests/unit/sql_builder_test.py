# pylint: disable=missing-function-docstring, missing-module-docstring
import logging

from pyspark.sql import SparkSession

from discoverx.config import Rule, ColumnInfo, TableInfo
from discoverx.sql_builder import SqlBuilder


def test_generate_sql():
    columns = [ColumnInfo("id", "number", False), ColumnInfo("name", "string", False)]
    table_info = TableInfo("meta", "db", "tb", columns)
    rules = [Rule("any_word", "regex", "Any word", "\w.")]

    expected = r"""SELECT
    'meta' as catalog,
    'db' as database,
    'tb' as table,
    column,
    rule_name,
    (sum(value) / count(value)) as frequency
FROM
(
    SELECT column, stack(1, 'any_word', any_word) as (rule_name, value)
    FROM
    (
        SELECT
        column,
        INT(regexp_like(value, '\w.')) AS any_word
        FROM (
            SELECT
                stack(1, 'name', name) AS (column, value)
            FROM db.tb
            TABLESAMPLE (100 ROWS)
        )
    )
)
GROUP BY catalog, database, table, column, rule_name"""

    actual = SqlBuilder().rule_matching_sql(table_info, rules, 100)

    logging.info("Generated SQL is: \n%s", actual)

    assert actual == expected


def test_generate_sql_multiple_rules():
    columns = [ColumnInfo("id", "number", False), ColumnInfo("name", "string", False)]
    table_info = TableInfo("meta", "db", "tb", columns)
    rules = [
        Rule("any_word", "regex", "Any word", "\w."),
        Rule("any_number", "regex", "Any number", "\d."),
    ]

    expected = r"""SELECT
    'meta' as catalog,
    'db' as database,
    'tb' as table,
    column,
    rule_name,
    (sum(value) / count(value)) as frequency
FROM
(
    SELECT column, stack(2, 'any_word', any_word, 'any_number', any_number) as (rule_name, value)
    FROM
    (
        SELECT
        column,
        INT(regexp_like(value, '\w.')) AS any_word,
        INT(regexp_like(value, '\d.')) AS any_number
        FROM (
            SELECT
                stack(1, 'name', name) AS (column, value)
            FROM db.tb
            TABLESAMPLE (100 ROWS)
        )
    )
)
GROUP BY catalog, database, table, column, rule_name"""

    actual = SqlBuilder().rule_matching_sql(table_info, rules, 100)

    logging.info("Generated SQL is: \n%s", actual)

    assert actual == expected


def test_sql_runs(spark: SparkSession):
    columns = [
        ColumnInfo("id", "number", False),
        ColumnInfo("ip", "string", False),
        ColumnInfo("description", "string", False),
    ]
    table_info = TableInfo("hive_metastore", "default", "tb_1", columns)
    rules = [
        Rule("any_word", "regex", "Any word", "\w+"),
        Rule("any_number", "regex", "Any number", "\d+"),
    ]

    actual = SqlBuilder().rule_matching_sql(table_info, rules, 100)

    logging.info("Generated SQL is: \n%s", actual)

    expected = spark.sql(actual).collect()

    print(expected)
